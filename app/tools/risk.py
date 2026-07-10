from __future__ import annotations

from typing import Any

from app.domain import ToolDefinition, ToolType

RISK_LABEL_ORDER = (
    "shell",
    "filesystem",
    "browser",
    "network",
    "mcp",
    "credentials",
    "graph",
    "memory",
    "read_only",
    "context_pack",
    "requires_approval",
    "sandbox_required",
    "dangerous",
    "mutation",
    "local_privileged_execution",
)

_CONTRACT_BASE_RISK_LABELS: dict[str, set[str]] = {
    "sandbox-edit": {"filesystem", "requires_approval", "sandbox_required", "mutation", "local_privileged_execution"},
    "agency.command.run": {
        "shell",
        "filesystem",
        "requires_approval",
        "sandbox_required",
        "dangerous",
        "local_privileged_execution",
    },
    "agency.file.write-text": {"filesystem", "mutation", "local_privileged_execution"},
    "agency.document.markdown-to-word": {"filesystem", "mutation", "local_privileged_execution"},
    "agency.excel.write-text": {"filesystem", "mutation", "local_privileged_execution"},
    "agency.excel.write-json": {"filesystem", "mutation", "local_privileged_execution"},
    "agency.excel.write-image": {"filesystem", "mutation", "local_privileged_execution"},
    "agency.media.publish": {"filesystem", "network", "requires_approval", "mutation", "local_privileged_execution"},
    "agency.media.send": {"filesystem", "network", "credentials", "requires_approval", "mutation"},
    "agency.voice.generate": {"filesystem", "requires_approval", "mutation", "local_privileged_execution"},
    "agency.http.request": {"network", "credentials", "mutation"},
    "agency.workflow.run": {"requires_approval", "mutation"},
    "agency.workflow.propose-create": {"requires_approval", "mutation"},
    "agency.workflow.propose-update": {"requires_approval", "mutation"},
    "agency.tool.propose-create": {"requires_approval", "mutation"},
    "agency.tool.propose-update": {"requires_approval", "mutation"},
    "agency.agent.propose-update": {"requires_approval", "mutation"},
    "agency.memory.remember": {"mutation"},
    "agency.memory.update": {"mutation"},
    "agency.memory.delete": {"mutation"},
    "agency.memory.exclusions.add": {"mutation"},
    "agency.memory.exclusions.delete": {"mutation"},
    "agency.workflow.memory-links.add": {"mutation"},
    "agency.workflow.memory-links.delete": {"mutation"},
    "agency.graph.context": {"graph", "read_only"},
    "agency.graph.search": {"graph", "read_only"},
    "agency.graph.expand": {"graph", "read_only"},
    "agency.graph.neighbors": {"graph", "read_only"},
    "agency.graph.path": {"graph", "read_only"},
    "agency.graph.summarize-subgraph": {"graph", "read_only"},
    "agency.graph.working-set.create": {"graph", "mutation"},
    "agency.graph.working-set.add": {"graph", "mutation"},
    "agency.graph.working-set.remove": {"graph", "mutation"},
    "agency.graph.working-set.summarize": {"graph", "read_only"},
    "agency.graph.working-set.clear": {"graph", "mutation"},
    "agency.graph.working-set.persist-context-pack": {"graph", "memory", "context_pack", "mutation"},
}

_BROWSER_MUTATION_TOOLS = {
    "agency.browser.click",
    "agency.browser.select-option",
    "agency.browser.type-text",
}

_COMPUTER_USE_MUTATION_TOOLS = {
    "click",
    "double-click",
    "drag",
    "key",
    "press-key",
    "select-option",
    "type",
    "type-text",
}


def risk_labels_for_tool_definition(tool: ToolDefinition) -> list[str]:
    labels: set[str] = set()
    security = tool.security
    tag_set = {str(tag).lower() for tag in tool.tags}
    if "graph" in tag_set or "agency_graph" in tag_set:
        labels.add("graph")
    if "memory" in tag_set:
        labels.add("memory")
    if "context_pack" in tag_set:
        labels.add("context_pack")
    if security.read_only or "read_only" in tag_set:
        labels.add("read_only")
    if security.allow_shell:
        labels.add("shell")
    if security.allow_filesystem:
        labels.add("filesystem")
    if security.allow_browser:
        labels.add("browser")
    if security.allow_network or tool.tool_type in {ToolType.HTTP_REQUEST, ToolType.A2A_REMOTE_AGENT}:
        labels.add("network")
    if tool.tool_type == ToolType.MCP_TOOL or security.allowlisted_mcp_servers:
        labels.add("mcp")
    if security.credential_references:
        labels.add("credentials")
    if security.requires_approval:
        labels.add("requires_approval")
    if security.sandbox_required:
        labels.add("sandbox_required")
    if security.dangerous:
        labels.add("dangerous")
    if tool.implementation.config.get("tool_family") == "computer_use":
        labels.add("browser")
        canonical_name = str(tool.implementation.config.get("canonical_tool_name") or tool.name).lower()
        if canonical_name in _COMPUTER_USE_MUTATION_TOOLS:
            labels.add("mutation")
    if _definition_can_mutate(tool):
        labels.add("mutation")
    if labels.intersection({"shell", "filesystem", "browser"}):
        labels.add("local_privileged_execution")
    return ordered_risk_labels(labels)


def risk_labels_for_contract_run(tool_name: str, payload: dict[str, Any] | None = None) -> list[str]:
    labels = set(_CONTRACT_BASE_RISK_LABELS.get(tool_name, set()))
    if tool_name.startswith("agency.browser."):
        labels.update({"browser", "local_privileged_execution"})
        if tool_name == "agency.browser.open":
            labels.add("network")
        if tool_name in _BROWSER_MUTATION_TOOLS:
            labels.add("mutation")
    if tool_name.startswith("agency.excel."):
        labels.update({"filesystem", "mutation", "local_privileged_execution"})
    if tool_name.startswith("agency.memory.") and tool_name not in {
        "agency.memory.list",
        "agency.memory.catalog",
        "agency.memory.exclusions.list",
    }:
        labels.add("mutation")
    if tool_name == "agency.http.request":
        if payload is not None:
            labels = {"network"}
        method = str((payload or {}).get("method") or "GET").upper()
        credential_mode = str((payload or {}).get("credential_mode") or "none").lower()
        labels.add("network")
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            labels.add("mutation")
        if credential_mode == "onecli":
            labels.add("credentials")
    if tool_name == "agency.media.send":
        labels.update({"network", "mutation"})
        if (payload or {}).get("credential_id"):
            labels.add("credentials")
        if (payload or {}).get("file_path"):
            labels.update({"filesystem", "local_privileged_execution"})
        if not bool((payload or {}).get("dry_run", True)):
            labels.add("requires_approval")
    if tool_name == "agency.media.publish":
        labels.update({"filesystem", "network", "mutation", "local_privileged_execution"})
        if not bool((payload or {}).get("dry_run", True)):
            labels.add("requires_approval")
    if tool_name == "agency.voice.generate":
        labels.update({"filesystem", "mutation", "local_privileged_execution"})
        if not bool((payload or {}).get("dry_run", True)):
            labels.add("requires_approval")
    return ordered_risk_labels(labels)


def risk_metadata_for_contract_run(tool_name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    labels = risk_labels_for_contract_run(tool_name, payload)
    return {
        "riskLabels": labels,
        "localPrivilegedExecution": "local_privileged_execution" in labels,
    }


def ordered_risk_labels(labels: set[str]) -> list[str]:
    ordered = [label for label in RISK_LABEL_ORDER if label in labels]
    ordered.extend(sorted(labels.difference(RISK_LABEL_ORDER)))
    return ordered


def _definition_can_mutate(tool: ToolDefinition) -> bool:
    if tool.security.read_only:
        return False
    if tool.security.allow_shell or tool.security.allow_filesystem:
        return True
    if tool.tool_type in {ToolType.SHELL_COMMAND, ToolType.HTTP_REQUEST, ToolType.WORKFLOW_TOOL,
                          ToolType.HUMAN_APPROVAL}:
        return True
    return tool.id in _CONTRACT_BASE_RISK_LABELS and "mutation" in _CONTRACT_BASE_RISK_LABELS[tool.id]
