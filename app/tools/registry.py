"""Runtime dispatch registry for enabled tool definitions."""

from __future__ import annotations

from jsonschema import validate
from typing import Any

from app.domain import ToolDefinition, ToolType
from app.protocols.mcp.registry import MCPClientRegistry
from app.runtime.native.approvals import ApprovalManager
from app.runtime.native.errors import ToolExecutionError
from app.tools.contracts.builtins import generated_builtin_contracts
from app.tools.contracts.models import ToolContract
from app.tools.contracts.registry import ToolContractRegistry
from .definitions import get_tool_catalog_specs
from .discovery import discover_allowed_python_tool_modules
from .executors import (
    A2ARemoteAgentToolExecutor,
    HttpRequestToolExecutor,
    HumanApprovalToolExecutor,
    McpToolExecutor,
    PythonFunctionToolExecutor,
    ShellCommandToolExecutor,
    SqlQueryToolExecutor,
    ToolExecutionContext,
    WorkflowToolExecutor,
)


class ToolRegistry:
    """Validate tool inputs and route executions to the matching executor implementation."""

    def __init__(
            self,
            contracts: list[ToolContract] | ToolContractRegistry | None = None,
            *,
            approval_manager: ApprovalManager | None = None,
            runtime_registry: Any | None = None,
            mcp_registry: MCPClientRegistry | None = None,
            execution_store: Any | None = None,
            tool_repository: Any | None = None,
    ):
        if isinstance(contracts, ToolContractRegistry):
            self.contract_registry = contracts
        else:
            contract_list = list(contracts or [])
            existing_contract_names = {contract.name for contract in contract_list}
            contract_list.extend(generated_builtin_contracts(existing_names=existing_contract_names))
            self.contract_registry = ToolContractRegistry(contract_list)
        app_modules = discover_allowed_python_tool_modules()
        migrated_modules = sorted(
            {
                spec.tool_definition.implementation.module
                for spec in get_tool_catalog_specs().values()
            }
        )
        self.approval_manager = approval_manager
        self.runtime_registry = runtime_registry
        self.mcp_registry = mcp_registry
        self.execution_store = execution_store
        self.tool_repository = tool_repository
        self.default_python_allowlist = ["tests.native_test_tools", *app_modules, *migrated_modules]
        self._executors: dict[ToolType, Any] = {
            ToolType.PYTHON_FUNCTION: PythonFunctionToolExecutor(self.default_python_allowlist),
            ToolType.HTTP_REQUEST: HttpRequestToolExecutor(),
            ToolType.SQL_QUERY: SqlQueryToolExecutor(),
            ToolType.SHELL_COMMAND: ShellCommandToolExecutor(),
            ToolType.MCP_TOOL: McpToolExecutor(),
            ToolType.A2A_REMOTE_AGENT: A2ARemoteAgentToolExecutor(),
            ToolType.WORKFLOW_TOOL: WorkflowToolExecutor(),
            ToolType.HUMAN_APPROVAL: HumanApprovalToolExecutor(),
        }

    def register_executor(self, tool_type: ToolType, executor: Any) -> None:
        self._executors[tool_type] = executor

    def validate_input(self, tool: ToolDefinition, arguments: dict[str, Any]) -> None:
        validate(instance=arguments, schema=tool.input_schema)

    async def list_enabled_tools(self) -> list[ToolDefinition]:
        if self.tool_repository is None:
            return []
        return await self.tool_repository.list()

    async def get_tool_definition(self, tool_id: str) -> ToolDefinition | None:
        if self.tool_repository is None:
            return None
        return await self.tool_repository.get(tool_id)

    async def execute(
            self,
            tool: ToolDefinition,
            arguments: dict[str, Any],
            *,
            execution_id: str,
            workflow_id: str | None = None,
            task_id: str | None = None,
            agent_id: str | None = None,
            tool_call_id: str | None = None,
            connector_binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.validate_input(tool, arguments)
        executor = self._executors.get(tool.tool_type)
        if executor is None:
            raise ToolExecutionError(f"Unsupported tool type '{tool.tool_type.value}'")
        context = ToolExecutionContext(
            execution_id=execution_id,
            workflow_id=workflow_id,
            task_id=task_id,
            agent_id=agent_id,
            tool_call_id=tool_call_id,
            runtime_registry=self.runtime_registry,
            approval_manager=self.approval_manager,
            mcp_registry=self.mcp_registry,
            execution_store=self.execution_store,
            connector_binding=connector_binding,
        )
        if getattr(executor, "async_execution", False):
            return await executor.aexecute(tool, arguments, context)
        return executor.execute(tool, arguments, context)
