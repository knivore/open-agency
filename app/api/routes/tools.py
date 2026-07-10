from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from typing import Any, Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user_if_present
from app.domain import ToolDefinition
from app.runtime.native.errors import ToolExecutionError
from app.services.generated_tool_workspace import GeneratedToolWorkspaceError, GeneratedToolWorkspaceService
from app.tools.module_visibility import tool_definition_visible_with_enabled_modules
from ._crud import build_crud_router


class ToolTestRequest(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)


class ToolValidateRequest(BaseModel):
    tool_definition: ToolDefinition = Field(alias="toolDefinition")


class GeneratedToolPackageScaffoldRequest(BaseModel):
    package_id: str = Field(alias="packageId", min_length=1)
    name: str = Field(min_length=1)
    description: str | None = None
    function_name: str | None = Field(default=None, alias="functionName")
    overwrite: bool = False


class GeneratedToolPublishRequest(BaseModel):
    package_id: str = Field(alias="packageId", min_length=1)
    tool_id: str = Field(alias="toolId", min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    callable_name: str = Field(alias="callableName", min_length=1)
    display_name: str | None = Field(default=None, alias="displayName")
    input_schema: dict[str, Any] = Field(alias="inputSchema", default_factory=dict)
    output_schema: dict[str, Any] = Field(alias="outputSchema", default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    security: dict[str, Any] | None = None


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
            response_filter=tool_definition_visible_with_enabled_modules,
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

    @router.get("/tools/generated/packages", tags=["Tools"], summary="List Generated Tool Packages")
    async def list_generated_tool_packages(request: Request, package_id: str | None = None):
        await resolve_current_user_if_present(request, context, required_scopes=["tools:read"])
        service = GeneratedToolWorkspaceService(context)
        result = await service.list_packages_with_registry()
        if package_id:
            result["packages"] = [
                item for item in result.get("packages", []) if str(item.get("package_id") or "") == package_id
            ]
            result["count"] = len(result["packages"])
        return result

    @router.get("/tools/generated/packages/{package_id}", tags=["Tools"], summary="Inspect Generated Tool Package")
    async def inspect_generated_tool_package(package_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["tools:read"])
        service = GeneratedToolWorkspaceService(context)
        try:
            return await service.inspect_package(package_id)
        except GeneratedToolWorkspaceError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.post("/tools/generated/packages/scaffold", tags=["Tools"], summary="Scaffold Generated Tool Package")
    async def scaffold_generated_tool_package(payload: GeneratedToolPackageScaffoldRequest, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["tools:write"])
        service = GeneratedToolWorkspaceService(context)
        try:
            result = service.scaffold_package(
                package_id=payload.package_id,
                name=payload.name,
                description=payload.description,
                function_name=payload.function_name,
                overwrite=payload.overwrite,
            )
            return {
                "ok": True,
                "package": result,
            }
        except GeneratedToolWorkspaceError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @router.post("/tools/generated/packages/publish", tags=["Tools"], summary="Publish Generated Tool")
    async def publish_generated_tool(payload: GeneratedToolPublishRequest, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["tools:write"])
        service = GeneratedToolWorkspaceService(context)
        try:
            tool = await service.publish_tool(
                package_id=payload.package_id,
                tool_id=payload.tool_id,
                name=payload.name,
                description=payload.description,
                callable_name=payload.callable_name,
                display_name=payload.display_name,
                input_schema=payload.input_schema,
                output_schema=payload.output_schema,
                tags=payload.tags,
                security=payload.security,
            )
            return {
                "ok": True,
                "tool": tool.model_dump(mode="json"),
            }
        except GeneratedToolWorkspaceError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return router
