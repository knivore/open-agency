from __future__ import annotations

import importlib
import inspect
from typing import Any

from app.domain import ToolDefinition, ToolType
from app.runtime.native.errors import ToolExecutionError
from .base import ToolExecutionContext


class PythonFunctionToolExecutor:
    tool_type = ToolType.PYTHON_FUNCTION
    async_execution = True

    def __init__(self, default_module_allowlist: list[str] | None = None):
        self.default_module_allowlist = default_module_allowlist or []

    async def aexecute(self, tool: ToolDefinition, arguments: dict[str, Any], context: ToolExecutionContext) -> dict[
        str, Any]:
        module_name = tool.implementation.target
        callable_name = tool.implementation.callable_name or tool.implementation.entrypoint or "run"
        module_allowlist = tool.security.module_allowlist or self.default_module_allowlist
        function_allowlist = tool.security.function_allowlist or tool.implementation.config.get("function_allowlist",
                                                                                                [])

        if not module_allowlist:
            raise ToolExecutionError(f"Python tool '{tool.id}' is missing a module allowlist")
        if module_name not in module_allowlist and not any(
                module_name.startswith(f"{prefix}.") for prefix in module_allowlist):
            raise ToolExecutionError(f"Module '{module_name}' is not allowlisted for tool '{tool.id}'")
        if function_allowlist and callable_name not in function_allowlist:
            raise ToolExecutionError(f"Function '{callable_name}' is not allowlisted for tool '{tool.id}'")

        module = importlib.import_module(module_name)
        if not hasattr(module, callable_name):
            raise ToolExecutionError(f"Tool function '{callable_name}' was not found in module '{module_name}'")
        function = getattr(module, callable_name)
        call_arguments = dict(arguments)
        signature = inspect.signature(function)
        accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD
                             for parameter in signature.parameters.values())
        if "tool_context" in signature.parameters or accepts_kwargs:
            call_arguments["tool_context"] = context
        result = function(**call_arguments)
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, dict) else {"result": result}
