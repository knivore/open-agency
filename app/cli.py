from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from app.api.context import get_default_api_context
from app.core.config import get_settings
from app.services.main_agent_setup import (
    MainAgentModelProfileRequiredError,
    MainAgentSetupInvalidError,
    MainAgentSetupRequiredError,
    MainAgentSetupService,
)
from app.services.main_agent_setup.prompt_doc import extract_prompt_from_doc
from app.tools.cli_discovery import (
    describe_tool,
    list_builtin_tool_definitions,
    resolve_tool,
    schema_for_tool,
    suggest_tool_ids,
    summarize_tool,
)
from app.tools.contracts.registry import get_default_contract_registry
from app.tools.contracts.validator import ToolContractValidationError
from app.tools.runtime import ToolRuntimeExecutor


async def _setup_main_agent(*, interactive: bool) -> int:
    context = get_default_api_context()
    service = MainAgentSetupService(context)
    try:
        profile = await service.ensure_startup_ready(
            interactive=interactive,
            settings=get_settings(),
            default_agent_instructions=extract_prompt_from_doc(),
        )
    except (MainAgentModelProfileRequiredError, MainAgentSetupRequiredError, MainAgentSetupInvalidError) as exc:
        print(service.startup_guidance(exc))
        return 1
    print(f"Active main-agent profile: {profile.id}")
    return 0


async def _check_main_agent() -> int:
    context = get_default_api_context()
    service = MainAgentSetupService(context)
    try:
        profile = await service.require_active_main_agent_profile()
    except (MainAgentModelProfileRequiredError, MainAgentSetupRequiredError, MainAgentSetupInvalidError) as exc:
        print(service.startup_guidance(exc))
        return 1
    print(
        "Main-agent setup is complete.\n"
        f"  profile_id: {profile.id}\n"
        f"  agent_id: {profile.agent_id}\n"
        f"  workflow_id: {profile.default_workflow_id}\n"
    )
    return 0


async def _sync_main_agent_prompt() -> int:
    context = get_default_api_context()
    service = MainAgentSetupService(context)
    try:
        agent = await service.sync_active_main_agent_instructions(extract_prompt_from_doc())
    except (MainAgentModelProfileRequiredError, MainAgentSetupRequiredError, MainAgentSetupInvalidError) as exc:
        print(service.startup_guidance(exc))
        return 1
    print(f"Synced main-agent instructions from docs/main-agent.md for agent: {agent.id}")
    return 0


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _tool_list(*, as_json: bool) -> int:
    tools = list_builtin_tool_definitions()
    summaries = [summarize_tool(tool) for tool in tools]
    if as_json:
        _print_json({"items": summaries, "count": len(summaries)})
        return 0

    print(f"{len(summaries)} tools")
    for item in summaries:
        side_effects = ", ".join(item["side_effects"])
        print(f"{item['id']}\t{item['command_alias']}\t{item['category']}\t{side_effects}\t{item['description']}")
    return 0


def _tool_describe(identifier: str, *, as_json: bool) -> int:
    tools = list_builtin_tool_definitions()
    tool = resolve_tool(identifier, tools)
    if tool is None:
        suggestions = suggest_tool_ids(identifier, tools)
        print(f"Unknown tool: {identifier}", file=sys.stderr)
        if suggestions:
            print("Did you mean:", file=sys.stderr)
            for suggestion in suggestions:
                print(f"  {suggestion}", file=sys.stderr)
        else:
            print("Run `agency tool list` to see available tools.", file=sys.stderr)
        return 1

    payload = describe_tool(tool)
    if as_json:
        _print_json(payload)
        return 0

    print(f"ID: {payload['id']}")
    print(f"Name: {payload['name']}")
    print(f"Command alias: {payload['command_alias']}")
    print(f"Category: {payload['category']}")
    print(f"Type: {payload['tool_type']}")
    print(f"Side effects: {', '.join(payload['side_effects'])}")
    print(f"Tags: {', '.join(payload['tags']) if payload['tags'] else 'none'}")
    print()
    print(payload["description"])
    print()
    print("Input schema:")
    _print_json(payload["input_schema"])
    print("Output schema:")
    _print_json(payload["output_schema"])
    return 0


def _tool_schema(identifier: str, *, which: str, as_json: bool) -> int:
    tools = list_builtin_tool_definitions()
    tool = resolve_tool(identifier, tools)
    if tool is None:
        suggestions = suggest_tool_ids(identifier, tools)
        print(f"Unknown tool: {identifier}", file=sys.stderr)
        if suggestions:
            print("Did you mean:", file=sys.stderr)
            for suggestion in suggestions:
                print(f"  {suggestion}", file=sys.stderr)
        else:
            print("Run `agency tool list` to see available tools.", file=sys.stderr)
        return 1

    payload = schema_for_tool(tool, which=which)
    if as_json:
        _print_json(payload)
        return 0

    print(f"Schema for {tool.id}:")
    _print_json(payload)
    return 0


def _parse_json_payload(raw_payload: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON payload: {exc.msg} at line {exc.lineno} column {exc.colno}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Tool run payload must be a JSON object.")
    return payload


def _resolve_contract_tool(identifier: str) -> str | None:
    registry = get_default_contract_registry()
    normalized = identifier.strip()
    if registry.has_contract(normalized):
        return normalized
    if normalized.startswith("agency."):
        normalized = normalized.removeprefix("agency.")
    normalized = normalized.replace(" ", "-")
    if registry.has_contract(normalized):
        return normalized
    return None


def _tool_run(identifier: str, *, raw_payload: str, actor: str | None, as_json: bool) -> int:
    tool_name = _resolve_contract_tool(identifier)
    if tool_name is None:
        contracts = [contract.name for contract in get_default_contract_registry().list_contracts()]
        print(f"Tool is not contract-runnable: {identifier}", file=sys.stderr)
        if contracts:
            print("Contract-backed tools:", file=sys.stderr)
            for contract in contracts:
                print(f"  {contract}", file=sys.stderr)
        else:
            print("No contract-backed tools are currently registered.", file=sys.stderr)
        return 1

    try:
        payload = _parse_json_payload(raw_payload)
        response = ToolRuntimeExecutor().run(tool_name, payload, actor=actor)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ToolContractValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    response_payload = response.model_dump(mode="json")
    if as_json:
        _print_json(response_payload)
    else:
        print(f"Tool: {tool_name}")
        print(f"Verdict: {response.verdict}")
        print(f"Dry run: {response.dryRun}")
        print(f"Signature: {response.signature}")
        if response.errors:
            print("Errors:")
            for error in response.errors:
                print(f"  {error}")
        if response.filesChanged:
            print("Files changed:")
            for file_changed in response.filesChanged:
                print(f"  {file_changed.path} ({file_changed.op})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agency management CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup_parser = subparsers.add_parser(
        "setup-main-agent",
        help="Run first-run setup for the default main agent and create provider/model profile if needed.",
    )
    setup_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Use MAIN_AGENT_BOOTSTRAP_* environment variables instead of interactive prompts.",
    )
    subparsers.add_parser(
        "check-main-agent",
        help="Check whether main-agent setup is complete.",
    )
    subparsers.add_parser(
        "sync-main-agent-prompt",
        help="Update the active main agent with the canonical prompt from docs/main-agent.md.",
    )
    tool_parser = subparsers.add_parser(
        "tool",
        help="Discover and inspect agent-facing tools.",
    )
    tool_subparsers = tool_parser.add_subparsers(dest="tool_command", required=True)

    tool_list_parser = tool_subparsers.add_parser("list", help="List registered built-in tools.")
    tool_list_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    tool_describe_parser = tool_subparsers.add_parser("describe", help="Show detailed guidance for one tool.")
    tool_describe_parser.add_argument("tool", help="Tool id, display name, or command alias.")
    tool_describe_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    tool_schema_parser = tool_subparsers.add_parser("schema", help="Show input and output schemas for one tool.")
    tool_schema_parser.add_argument("tool", help="Tool id, display name, or command alias.")
    tool_schema_parser.add_argument(
        "--which",
        choices=["input", "output", "both"],
        default="both",
        help="Select which schema to show.",
    )
    tool_schema_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    tool_run_parser = tool_subparsers.add_parser(
        "run",
        help="Run a contract-backed tool through the policy/runtime layer.",
    )
    tool_run_parser.add_argument("tool", help="Contract-backed tool name, for example sandbox-edit.")
    tool_run_parser.add_argument("--json", required=True, help="JSON object payload for the tool run.")
    tool_run_parser.add_argument("--actor", default="cli", help="Actor id to record on the tool run.")
    tool_run_parser.add_argument(
        "--output-json",
        action="store_true",
        help="Emit the full structured ToolRunResponse as JSON.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "setup-main-agent":
        interactive = not getattr(args, "non_interactive", False)
        return asyncio.run(_setup_main_agent(interactive=interactive))
    if args.command == "check-main-agent":
        return asyncio.run(_check_main_agent())
    if args.command == "sync-main-agent-prompt":
        return asyncio.run(_sync_main_agent_prompt())
    if args.command == "tool":
        if args.tool_command == "list":
            return _tool_list(as_json=args.json)
        if args.tool_command == "describe":
            return _tool_describe(args.tool, as_json=args.json)
        if args.tool_command == "schema":
            return _tool_schema(args.tool, which=args.which, as_json=args.json)
        if args.tool_command == "run":
            return _tool_run(args.tool, raw_payload=args.json, actor=args.actor, as_json=args.output_json)
        parser.error(f"Unknown tool command: {args.tool_command}")
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
