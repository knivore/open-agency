from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from typing import Any, Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user, resolve_current_user_if_present
from app.tools.contracts.validator import ToolContractValidationError
from app.tools.runtime.executor import ToolRuntimeExecutor

COMMAND_RUN_APPROVAL_HEADER = "x-agency-command-approved"
_TRUTHY_HEADER_VALUES = {"1", "true", "yes", "approved"}


def _command_run_approval_granted(request: Request) -> bool:
    value = request.headers.get(COMMAND_RUN_APPROVAL_HEADER)
    return bool(value and value.strip().lower() in _TRUTHY_HEADER_VALUES)


def create_tool_runtime_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    executor = ToolRuntimeExecutor(context=context)
    router = APIRouter(prefix="/tools", tags=["Tool Runtime"])

    @router.post("/{tool_name}/run", summary="Run Tool")
    async def run_tool(tool_name: str, payload: dict[str, Any], request: Request):
        actor: str | None = None
        if tool_name == "agency.command.run":
            current_user = await resolve_current_user(request, context, required_scopes=["tools:write"])
            if not _command_run_approval_granted(request):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        "agency.command.run requires explicit approval context; "
                        f"set {COMMAND_RUN_APPROVAL_HEADER}: true."
                    ),
                )
            actor = f"approved/{current_user.id}"
        else:
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
