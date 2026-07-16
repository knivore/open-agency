from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from typing import Any, Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user, resolve_current_user_if_present
from app.tools.contracts.validator import ToolContractValidationError
from app.tools.runtime.executor import ToolRuntimeExecutor

def create_tool_runtime_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    executor = ToolRuntimeExecutor(context=context)
    router = APIRouter(prefix="/tools", tags=["Tool Runtime"])

    @router.post("/{tool_name}/run", summary="Run Tool")
    async def run_tool(tool_name: str, payload: dict[str, Any], request: Request):
        actor: str | None = None
        method = str(payload.get("method") or "GET").upper()
        requires_execution_approval = (
            tool_name == "agency.command.run"
            or tool_name == "agency.file.write-text"
            or tool_name.startswith("agency.excel.")
            or tool_name == "agency.document.markdown-to-word"
            or (tool_name.startswith("agency.browser.") and tool_name != "agency.browser.open")
            or (tool_name == "agency.http.request" and method in {"POST", "PUT", "PATCH", "DELETE"})
        )
        requires_execution_actor = (
            tool_name == "agency.workflow.run" or tool_name.startswith("agency.execution.")
        )
        if requires_execution_approval:
            await resolve_current_user(request, context, required_scopes=["tools:write"])
            # Direct HTTP requests cannot prove that a human approved this exact
            # invocation. State-changing tools remain available through durable,
            # invocation-bound execution approvals.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Direct state-changing tool execution is disabled; use an execution-bound approval request.",
            )
        elif requires_execution_actor:
            # Execution contracts operate on user-owned runtime state, so an
            # optional audit actor is not a sufficient authorization boundary.
            current_user = await resolve_current_user(request, context, required_scopes=["tools:write"])
            actor = current_user.id
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
