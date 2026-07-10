"""Static capability metadata exposed to frontend clients."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.config import get_settings
from app.modules.registry import optional_module_capabilities, optional_module_specs
from app.tools.contracts.registry import get_default_contract_registry
from app.tools.module_visibility import tool_name_hidden_by_disabled_modules


def _module_tool_names_by_attr(attr_name: str) -> dict[str, set[str]]:
    return {
        spec.key: set(getattr(spec, attr_name))
        for spec in optional_module_specs()
        if getattr(spec, attr_name)
    }


def _optional_module_tool_names(attr_name: str) -> set[str]:
    names: set[str] = set()
    for module_names in _module_tool_names_by_attr(attr_name).values():
        names.update(module_names)
    return names


def _optional_module_keys_for_tool(tool_name: str, attr_name: str) -> set[str]:
    return {
        module_key
        for module_key, names in _module_tool_names_by_attr(attr_name).items()
        if tool_name in names
    }


STATIC_APPROVAL_TOOLS = {
    "agency.workflow.run",
    "agency.workflow.propose-create",
    "agency.workflow.propose-update",
    "agency.tool.propose-create",
    "agency.tool.propose-update",
    "agency.agent.propose-update",
}

CONVERSATION_CONTEXT_TOOLS = {
    "agency.workflow.propose-create",
    "agency.workflow.propose-update",
    "agency.tool.propose-create",
    "agency.tool.propose-update",
    "agency.agent.propose-update",
}

API_CONTEXT_TOOLS = {
    "agency.workflow.list",
    "agency.workflow.get",
    "agency.agent.list",
    "agency.agent.get",
    "agency.workflow.run",
    "agency.memory.list",
    "agency.memory.catalog",
    "agency.memory.remember",
    "agency.memory.update",
    "agency.memory.delete",
    "agency.memory.exclusions.list",
    "agency.memory.exclusions.add",
    "agency.memory.exclusions.delete",
    "agency.workflow.memory-links.list",
    "agency.workflow.memory-links.add",
    "agency.workflow.memory-links.delete",
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
        settings = get_settings()
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
                if not _hide_contract_from_capabilities(contract.name, settings=settings)
            ],
            "events": {
                "streamUrl": "/api/runtime/events/stream",
                "websocketUrl": "/ws/runtime/events",
            },
            "modules": _module_capabilities(),
        }

    return router


def _hide_contract_from_capabilities(tool_name: str, *, settings) -> bool:
    return tool_name_hidden_by_disabled_modules(tool_name)


def _visible_module_tool_names(tool_names: list[str]) -> list[str]:
    return [tool_name for tool_name in tool_names if not tool_name_hidden_by_disabled_modules(tool_name)]


def _module_capabilities() -> dict:
    """Expose coarse module availability so clients do not infer features from 404s."""
    return optional_module_capabilities(visible_tool_names=_visible_module_tool_names)


def _execution_metadata(tool_name: str) -> dict:
    return {
        "executionMode": _execution_mode(tool_name),
        "requiresConversation": tool_name in CONVERSATION_CONTEXT_TOOLS,
        "supportsApprovalRequest": tool_name in _approval_tools(),
        "inputContextFields": _input_context_fields(tool_name),
        "sideEffects": _side_effects(tool_name),
        "policyNotes": _policy_notes(tool_name),
    }


def _approval_tools() -> set[str]:
    # Optional module specs can be swapped at runtime in tests and deployments
    # through env/config refs, so keep module-derived tool metadata dynamic.
    return STATIC_APPROVAL_TOOLS | _optional_module_tool_names("mutating_tool_names")


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
    if tool_name in _approval_tools():
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
    if tool_name == "agency.speech.listen":
        effects.add("speech_input")
    if tool_name == "agency.speech.speak":
        effects.add("speech_output")
    if tool_name == "agency.speech.continue":
        effects.update({"speech_input", "conversation_continuation"})
    if tool_name == "agency.voice.generate":
        effects.update({"speech_output", "media_generation", "filesystem"})
    if tool_name.startswith("agency.excel.") or tool_name in {"agency.file.write-text",
                                                              "agency.document.markdown-to-word"}:
        effects.update({"write", "filesystem"})
    if tool_name.startswith("agency.memory."):
        effects.add("memory")
    for module_key in _optional_module_keys_for_tool(tool_name, "read_only_tool_names"):
        effects.update({f"optional_module:{module_key}", "module_read"})
    for module_key in _optional_module_keys_for_tool(tool_name, "mutating_tool_names"):
        # These labels let clients gate mutating module surfaces without
        # duplicating backend tool-security policy.
        effects.update({f"optional_module:{module_key}", "module_mutation", "approval_request"})
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
        notes.append(
            "Browser navigation is constrained by URL scheme, blocked metadata hosts, and host allowlist policy.")
    if tool_name == "agency.command.run":
        notes.append("Dangerous shell patterns are denied and unapproved actors receive a policy warning.")
    if tool_name == "agency.http.request":
        notes.append(
            "HTTP requests are constrained by URL scheme, method, host allowlist, metadata host, and TLS policy.")
    if tool_name == "agency.speech.listen":
        notes.append(
            "Speech listening stays on the Agency speech surface so channels can reuse the same transcription contract.")
    if tool_name == "agency.speech.speak":
        notes.append("Speech output stays generic so channels can reuse the same Agency speech contract.")
    if tool_name == "agency.speech.continue":
        notes.append(
            "Speech continuation routes follow-up speech through the shared Agency speech surface instead of a module-specific callback path.")
    if tool_name == "agency.voice.generate":
        notes.append(
            "Voice generation is local-first and returns reusable Agency storage media for downstream workflows, tools, or tied-application delivery.")
    read_modules = sorted(_optional_module_keys_for_tool(tool_name, "read_only_tool_names"))
    if read_modules:
        notes.append(f"Read-only optional-module tool. Enabled module(s): {', '.join(read_modules)}.")
    mutating_modules = sorted(_optional_module_keys_for_tool(tool_name, "mutating_tool_names"))
    if mutating_modules:
        notes.append(
            f"Mutating optional-module tool. Enabled module(s): {', '.join(mutating_modules)}; "
            "module-owned policy and approval checks still apply."
        )
    return notes
