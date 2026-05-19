from __future__ import annotations

from fastapi import APIRouter

from app.tools.contracts import get_default_contract_registry

APPROVAL_TOOLS = {
    "agency.workflow.run",
    "agency.workflow.propose-create",
    "agency.workflow.propose-update",
    "agency.tool.propose-create",
    "agency.tool.propose-update",
}

CONVERSATION_CONTEXT_TOOLS = {
    "agency.workflow.propose-create",
    "agency.workflow.propose-update",
    "agency.tool.propose-create",
    "agency.tool.propose-update",
}

API_CONTEXT_TOOLS = {
    "agency.workflow.list",
    "agency.workflow.get",
    "agency.workflow.run",
    "agency.memory.list",
    "agency.memory.remember",
    "agency.memory.update",
    "agency.memory.delete",
}

BROWSER_MUTATION_TOOLS = {
    "agency.browser.click",
    "agency.browser.select-option",
    "agency.browser.type-text",
}


def create_capabilities_router() -> APIRouter:
    router = APIRouter(tags=["Capabilities"])
    registry = get_default_contract_registry()

    @router.get("/capabilities", summary="Get Runtime Capabilities")
    async def get_capabilities():
        return {
            "name": "agency-runtime",
            "version": "1.0",
            "tools": [
                {
                    "name": contract.name,
                    "version": contract.version,
                    "description": contract.description,
                    "contractUrl": f"/tools/contracts/{contract.name}",
                    "runUrl": f"/tools/{contract.name}/run",
                    "execution": _execution_metadata(contract.name),
                }
                for contract in registry.list_contracts()
            ],
            "events": {
                "streamUrl": "/api/runtime/events/stream",
                "websocketUrl": "/ws/runtime/events",
            },
        }

    return router


def _execution_metadata(tool_name: str) -> dict:
    return {
        "executionMode": _execution_mode(tool_name),
        "requiresConversation": tool_name in CONVERSATION_CONTEXT_TOOLS,
        "supportsApprovalRequest": tool_name in APPROVAL_TOOLS,
        "inputContextFields": _input_context_fields(tool_name),
        "sideEffects": _side_effects(tool_name),
        "policyNotes": _policy_notes(tool_name),
    }


def _execution_mode(tool_name: str) -> str:
    if tool_name in CONVERSATION_CONTEXT_TOOLS:
        return "conversation_context"
    if tool_name == "agency.workflow.run":
        return "approval_context"
    if tool_name in API_CONTEXT_TOOLS:
        return "api_context"
    return "direct"


def _input_context_fields(tool_name: str) -> list[str]:
    fields: list[str] = ["actor"]
    if tool_name in APPROVAL_TOOLS:
        fields.extend(["conversation_id", "origin_message_id"])
    if tool_name in {"agency.human.ask"}:
        fields.extend(["process_id", "timeout_seconds"])
    return fields


def _side_effects(tool_name: str) -> list[str]:
    effects: set[str] = set()
    if tool_name == "sandbox-edit":
        effects.update({"write", "filesystem"})
    if tool_name == "agency.command.run":
        effects.add("shell")
    if tool_name.startswith("agency.browser."):
        effects.update({"browser", "network"})
    if tool_name in BROWSER_MUTATION_TOOLS:
        effects.add("browser_mutation")
    if tool_name == "agency.http.request":
        effects.add("network")
    if tool_name.startswith("agency.excel.") or tool_name in {"agency.file.write-text", "agency.document.markdown-to-word"}:
        effects.update({"write", "filesystem"})
    if tool_name.startswith("agency.memory."):
        effects.add("memory")
    if tool_name == "agency.workflow.run":
        effects.update({"workflow_execution", "approval_request"})
    if tool_name in CONVERSATION_CONTEXT_TOOLS:
        effects.add("approval_request")
    if tool_name == "agency.human.ask":
        effects.add("human_interaction")
    return sorted(effects) or ["read"]


def _policy_notes(tool_name: str) -> list[str]:
    notes: list[str] = []
    if tool_name == "agency.workflow.run":
        notes.append("Protected workflows require conversation_id to create an approval request.")
    if tool_name in CONVERSATION_CONTEXT_TOOLS:
        notes.append("Requires conversation_id to create an approval request.")
    if tool_name in BROWSER_MUTATION_TOOLS:
        notes.append("Mutating browser actions warn unless actor uses an approved/ prefix.")
    if tool_name == "agency.browser.open":
        notes.append("Browser navigation is constrained by URL scheme, blocked metadata hosts, and host allowlist policy.")
    if tool_name == "agency.command.run":
        notes.append("Dangerous shell patterns are denied and unapproved actors receive a policy warning.")
    if tool_name == "agency.http.request":
        notes.append("HTTP requests are constrained by URL scheme, method, host allowlist, metadata host, and TLS policy.")
    return notes
