from __future__ import annotations

from copy import deepcopy

from app.tools.cli_discovery import list_builtin_tool_definitions

from .models import ToolContract


CONTRACT_CONTEXT = "https://agency.local/tool-contracts/v1"
CONTRACT_TYPE = "ToolContract"


def generated_builtin_contracts(*, existing_names: set[str] | None = None) -> list[ToolContract]:
    existing = existing_names or set()
    contracts: list[ToolContract] = []
    for tool in list_builtin_tool_definitions():
        if tool.id in existing:
            continue
        contracts.append(
            ToolContract(
                context=CONTRACT_CONTEXT,
                type=CONTRACT_TYPE,
                name=tool.id,
                version="1.0",
                description=_contract_description(tool.description),
                inputs=deepcopy(tool.input_schema),
                outputs=deepcopy(_generic_tool_run_response_schema()),
            )
        )
    return contracts


def _contract_description(description: str) -> str:
    return (
        f"{description} This contract is generated from the canonical built-in ToolDefinition so agents can "
        "discover, validate, and receive a signed ToolRunResponse for the tool. Tools that require browser, "
        "human, workflow-session, or other runtime context return a structured requires_runtime_context response "
        "until their specialized executor is bridged into the contract runtime."
    )


def _generic_tool_run_response_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["ok", "warn", "deny"]},
            "policyVerdict": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {
                            "score": {"type": "integer"},
                            "rules": {"type": "array", "items": {"type": "object"}},
                        },
                        "additionalProperties": True,
                    },
                    {"type": "null"},
                ]
            },
            "result": {
                "anyOf": [
                    {"type": "object", "additionalProperties": True},
                    {"type": "null"},
                ]
            },
            "patch": {"type": ["string", "null"]},
            "filesChanged": {"type": "array", "items": {"type": "object"}},
            "errors": {"type": "array", "items": {"type": "string"}},
            "dryRun": {"type": "boolean"},
            "timestamp": {"type": "string"},
            "actor": {"type": ["string", "null"]},
            "signature": {"type": ["string", "null"]},
        },
        "required": ["verdict", "dryRun", "timestamp"],
        "additionalProperties": False,
    }
