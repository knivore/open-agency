from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Set

from app.api.context import ApiContext
from app.domain import ToolType, WorkflowDefinition
from app.domain.tools import ToolDefinition
from app.integrations.connectors import normalize_connector_provider_key


@dataclass
class ValidationIssue:
    code: str
    message: str
    severity: str = "error"
    node_id: str | None = None
    field: str | None = None


@dataclass
class WorkflowValidationResult:
    nodes: List[Dict[str, Any]]
    edges: List[Dict[str, Any]]
    validation_errors: List[Dict[str, Any]] = field(default_factory=list)
    validation_warnings: List[Dict[str, Any]] = field(default_factory=list)
    available_tools: List[Dict[str, Any]] = field(default_factory=list)
    available_agents: List[Dict[str, Any]] = field(default_factory=list)
    compatible_runtime_adapters: List[Dict[str, Any]] = field(default_factory=list)


class WorkflowValidationService:
    def __init__(self, context: ApiContext):
        self.context = context

    async def validate(self, workflow: WorkflowDefinition) -> WorkflowValidationResult:
        await self.context.ensure_runtime_adapter_seed_data()
        available_agents = await self.context.agent_repo.list()
        available_tools = await self.context.tool_repo.list()
        available_profiles = await self.context.model_profile_repo.list()
        runtime_adapters = await self.context.runtime_adapter_repo.list()
        mcp_servers = await self.context.mcp_server_repo.list()

        local_agent_ids = {agent.id for agent in workflow.agent_definitions}
        local_tool_ids = {tool.id for tool in workflow.tool_definitions}
        available_agent_ids: Set[str] = local_agent_ids | {agent.id for agent in available_agents}
        available_tool_ids: Set[str] = local_tool_ids | {tool.id for tool in available_tools}
        available_profile_ids = {profile.id for profile in available_profiles}
        runtime_adapter_ids = {adapter.id for adapter in runtime_adapters}
        mcp_server_ids = {server.id for server in mcp_servers}

        errors: List[ValidationIssue] = []
        warnings: List[ValidationIssue] = []

        node_ids = {node.id for node in workflow.nodes}
        if workflow.entrypoint not in node_ids:
            errors.append(ValidationIssue(code="entrypoint.missing", message="Entrypoint node does not exist",
                                          field="entrypoint"))

        outgoing = {node_id: 0 for node_id in node_ids}
        incoming = {node_id: 0 for node_id in node_ids}
        for edge in workflow.edges:
            if edge.source_node_id not in node_ids or edge.target_node_id not in node_ids:
                errors.append(
                    ValidationIssue(code="edge.invalid", message="Edge references a missing node", field="edges"))
                continue
            outgoing[edge.source_node_id] += 1
            incoming[edge.target_node_id] += 1
            if edge.source_node_id == edge.target_node_id:
                warnings.append(ValidationIssue(code="edge.self_reference",
                                                message="Edge references the same node as source and target",
                                                severity="warning"))

        allow_orphans = bool(workflow.metadata.get("allow_orphan_nodes"))
        if not allow_orphans:
            for node in workflow.nodes:
                if node.id == workflow.entrypoint:
                    continue
                if incoming.get(node.id, 0) == 0:
                    warnings.append(
                        ValidationIssue(
                            code="node.orphaned",
                            message=f"Node '{node.name}' is orphaned",
                            severity="warning",
                            node_id=node.id,
                        )
                    )

        for node in workflow.nodes:
            if node.agent_id and node.agent_id not in available_agent_ids:
                errors.append(
                    ValidationIssue(code="node.agent.missing", message=f"Node '{node.name}' references a missing agent",
                                    node_id=node.id))
            if node.tool_id and node.tool_id not in available_tool_ids:
                errors.append(
                    ValidationIssue(code="node.tool.missing", message=f"Node '{node.name}' references a missing tool",
                                    node_id=node.id))

        for agent in workflow.agent_definitions:
            if agent.model_profile_id and agent.model_profile_id not in available_profile_ids:
                errors.append(ValidationIssue(code="agent.model_profile.missing",
                                              message=f"Agent '{agent.name}' references a missing model profile"))
            if agent.tool_ids and agent.model_profile_id:
                profile = next((item for item in available_profiles if item.id == agent.model_profile_id), None)
                if profile and not profile.supports_tools:
                    errors.append(ValidationIssue(code="agent.model_profile.incompatible",
                                                  message=f"Agent '{agent.name}' uses tools but selected model profile does not support tools"))
            for tool_id in agent.tool_ids:
                if tool_id not in available_tool_ids:
                    errors.append(ValidationIssue(code="agent.tool.missing",
                                                  message=f"Agent '{agent.name}' references a missing tool"))

        for task in workflow.task_definitions:
            if task.agent_id and task.agent_id not in available_agent_ids:
                errors.append(ValidationIssue(code="task.agent.missing",
                                              message=f"Task '{task.name}' references a missing agent"))
            for tool_id in task.tool_ids:
                if tool_id not in available_tool_ids:
                    errors.append(ValidationIssue(code="task.tool.missing",
                                                  message=f"Task '{task.name}' references a missing tool"))

        referenced_tool_ids: Set[str] = set()
        for node in workflow.nodes:
            if node.tool_id:
                referenced_tool_ids.add(node.tool_id)
        for agent in workflow.agent_definitions:
            referenced_tool_ids.update(agent.tool_ids)
        for task in workflow.task_definitions:
            referenced_tool_ids.update(task.tool_ids)
        all_tools = workflow.tool_definitions + [
            tool for tool in available_tools
            if tool.id in referenced_tool_ids and tool.id not in local_tool_ids
        ]
        workflow_connector_bindings = self._workflow_connector_bindings(workflow)
        workflow_connector_binding_providers = {
            str(binding.get("provider") or "").strip()
            for binding in workflow_connector_bindings
            if str(binding.get("provider") or "").strip()
        }
        for tool in all_tools:
            if not tool.input_schema:
                errors.append(ValidationIssue(code="tool.input_schema.missing",
                                              message=f"Tool '{tool.name}' is missing input_schema"))
            dangerous = any([
                tool.security.allow_shell,
                tool.security.allow_browser,
                tool.security.allow_filesystem,
                tool.security.allow_network,
            ])
            if dangerous and not (tool.security.sandbox_required or tool.security.approval_required):
                errors.append(ValidationIssue(code="tool.security.dangerous",
                                              message=f"Tool '{tool.name}' is dangerous and requires sandboxing or approval"))
            if tool.security.approval_required and not dangerous:
                warnings.append(
                    ValidationIssue(code="tool.security.approval", message=f"Tool '{tool.name}' requires approval",
                                    severity="warning"))
            if tool.tool_type == ToolType.HTTP_REQUEST and not tool.security.allowlisted_domains:
                errors.append(ValidationIssue(code="tool.http.allowlist.missing",
                                              message=f"HTTP tool '{tool.name}' is missing allowlisted domains"))
            if tool.tool_type == ToolType.A2A_REMOTE_AGENT:
                if not tool.security.allowlisted_domains:
                    errors.append(ValidationIssue(code="tool.a2a.allowlist.missing",
                                                  message=f"A2A remote tool '{tool.name}' is missing allowlisted domains"))
                if tool.implementation.config.get(
                        "stub_response") is None and not tool.implementation.target.startswith(("http://", "https://")):
                    errors.append(ValidationIssue(code="tool.a2a.target.invalid",
                                                  message=f"A2A remote tool '{tool.name}' is missing a valid HTTP endpoint target"))
            if tool.tool_type == ToolType.PYTHON_FUNCTION and not (
                    tool.security.module_allowlist
                    or tool.implementation.target.startswith("app.tools.implementations.")
                    or tool.implementation.target == "tests.native_test_tools"
            ):
                errors.append(ValidationIssue(code="tool.python.allowlist.missing",
                                              message=f"Python tool '{tool.name}' is missing a module allowlist"))
            if tool.tool_type == ToolType.MCP_TOOL:
                if not tool.security.allowlisted_mcp_servers:
                    errors.append(ValidationIssue(code="tool.mcp.allowlist.missing",
                                                  message=f"MCP tool '{tool.name}' is missing an allowlisted MCP server"))
                missing_servers = [server_id for server_id in tool.security.allowlisted_mcp_servers if
                                   server_id not in mcp_server_ids]
                if missing_servers:
                    errors.append(ValidationIssue(code="tool.mcp.server.missing",
                                                  message=f"MCP tool '{tool.name}' references unknown MCP servers: {', '.join(missing_servers)}"))
            if tool.tool_type == ToolType.SHELL_COMMAND:
                if not tool.security.allow_shell:
                    errors.append(
                        ValidationIssue(code="tool.shell.disabled", message=f"Shell tool '{tool.name}' is disabled"))
                if not tool.security.approval_required:
                    errors.append(ValidationIssue(code="tool.shell.approval.missing",
                                                  message=f"Shell tool '{tool.name}' requires approval"))
                if not tool.security.sandbox_required:
                    errors.append(ValidationIssue(code="tool.shell.sandbox.missing",
                                                  message=f"Shell tool '{tool.name}' requires sandboxing"))
            if self._is_connector_backed_tool(tool):
                provider_hint = self._connector_provider_hint(tool)
                tool_bindings = tool.security.connector_bindings
                if not tool_bindings:
                    if provider_hint and provider_hint not in workflow_connector_binding_providers:
                        errors.append(
                            ValidationIssue(
                                code="tool.connector_binding.missing",
                                message=(
                                    f"Connector-backed tool '{tool.name}' requires a connector binding for "
                                    f"provider '{provider_hint}'. Add ToolDefinition.security.connector_bindings "
                                    "or workflow.metadata.connector_bindings."
                                ),
                                field="connector_bindings",
                            )
                        )
                    elif not workflow_connector_bindings:
                        errors.append(
                            ValidationIssue(
                                code="tool.connector_binding.missing",
                                message=(
                                    f"Connector-backed tool '{tool.name}' requires a connector binding. "
                                    "Add ToolDefinition.security.connector_bindings or "
                                    "workflow.metadata.connector_bindings before approval."
                                ),
                                field="connector_bindings",
                            )
                        )
                    elif len(workflow_connector_bindings) > 1 and provider_hint is None:
                        warnings.append(
                            ValidationIssue(
                                code="tool.connector_binding.ambiguous",
                                message=(
                                    f"Connector-backed tool '{tool.name}' has multiple workflow connector defaults; "
                                    "the tool call should pass provider or credential_id to avoid runtime ambiguity."
                                ),
                                severity="warning",
                                field="connector_bindings",
                            )
                        )

        if workflow.default_runtime_adapter_id and workflow.default_runtime_adapter_id not in runtime_adapter_ids:
            errors.append(ValidationIssue(code="runtime_adapter.missing",
                                          message="Selected default runtime adapter does not exist"))

        if workflow.allowed_runtime_adapter_ids:
            missing = [item for item in workflow.allowed_runtime_adapter_ids if item not in runtime_adapter_ids]
            if missing:
                errors.append(ValidationIssue(code="runtime_adapter.allowed_missing",
                                              message=f"Allowed runtime adapters do not exist: {', '.join(missing)}"))
            if workflow.default_runtime_adapter_id and workflow.default_runtime_adapter_id not in workflow.allowed_runtime_adapter_ids:
                warnings.append(ValidationIssue(code="runtime_adapter.default_not_allowed",
                                                message="Default runtime adapter is not listed in allowed runtime adapters",
                                                severity="warning"))

        compatible_runtime_adapters = []
        for adapter in runtime_adapters:
            try:
                runtime_adapter = self.context.runtime_registry.get(adapter.id)
            except KeyError:
                continue
            if await runtime_adapter.supports(workflow):
                compatible_runtime_adapters.append(adapter.model_dump(mode="json"))

        return WorkflowValidationResult(
            nodes=[node.model_dump(mode="json") for node in workflow.nodes],
            edges=[edge.model_dump(mode="json") for edge in workflow.edges],
            validation_errors=[issue.__dict__ for issue in errors],
            validation_warnings=[issue.__dict__ for issue in warnings],
            available_tools=[tool.model_dump(mode="json") for tool in available_tools],
            available_agents=[agent.model_dump(mode="json") for agent in available_agents],
            compatible_runtime_adapters=compatible_runtime_adapters,
        )

    def _workflow_connector_bindings(self, workflow: WorkflowDefinition) -> list[dict[str, Any]]:
        bindings = workflow.metadata.get("connector_bindings") if isinstance(workflow.metadata, dict) else None
        if not isinstance(bindings, list):
            return []
        normalized: list[dict[str, Any]] = []
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            legacy_ref = str(binding.get("ref") or "").strip()
            credential_id = str(binding.get("credential_id") or legacy_ref or "").strip()
            provider = normalize_connector_provider_key(str(binding.get("provider") or ""))
            if not provider:
                purpose = str(binding.get("purpose") or "").strip()
                if purpose:
                    provider = normalize_connector_provider_key(purpose.split("_", 1)[0].split("-", 1)[0])
            if provider and credential_id:
                normalized.append({**binding, "provider": provider, "credential_id": credential_id})
        return normalized

    def _connector_provider_hint(self, tool: ToolDefinition) -> str | None:
        config = tool.implementation.config or {}
        for key in ("provider", "provider_key", "connector", "connector_provider"):
            value = config.get(key)
            if isinstance(value, str) and value.strip():
                return normalize_connector_provider_key(value.strip())
        return None

    def _is_connector_backed_tool(self, tool: ToolDefinition) -> bool:
        tags = {tag.strip().lower() for tag in tool.tags if isinstance(tag, str)}
        if tool.implementation.target == "agency.system.connector" or "system" in tags:
            return False
        if {"connector", "integration"} & tags:
            return True
        config = tool.implementation.config or {}
        return any(key in config for key in ("provider", "provider_key", "connector", "connector_provider"))
