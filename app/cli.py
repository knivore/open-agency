from __future__ import annotations

import argparse
import asyncio
import binascii
import json
import sys
from datetime import date, datetime
from pathlib import Path
from pydantic import ValidationError
from typing import Any

from app.api.context import get_default_api_context
from app.core.config import get_settings
from app.graph.backfill import GraphProjectionBackfillService
from app.graph.neo4j_projection import Neo4jGraphProjector, Neo4jProjectionConfig, create_neo4j_driver
from app.graph.parity import Neo4jGraphParityChecker
from app.graph.projection import GraphProjectionWorker
from app.graph.rebuild import Neo4jGraphRebuilder
from app.services.agent_markdown_import import (
    AgentImportBatchCommitRequest,
    AgentImportBatchPreviewRequest,
    AgentImportCommitRequest,
    AgentImportError,
    AgentImportPreviewRequest,
    AgentMarkdownImportService,
)
from app.services.channel_identity import ChannelIdentityMappingService
from app.services.connector_installations import ConnectorInstallationService
from app.services.connectors import ConnectorService
from app.services.conversations.core import ConversationService
from app.services.credentials import CredentialService
from app.services.main_agent_setup.prompt_doc import extract_prompt_from_doc
from app.services.main_agent_setup.service import (
    MainAgentModelProfileRequiredError,
    MainAgentSetupInvalidError,
    MainAgentSetupRequiredError,
    MainAgentSetupService,
)
from app.services.public_endpoints import PublicEndpointService
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
from app.tools.registry_config import (
    load_agency_tool_registry_config,
    load_system_runtime_tool_spec_config,
    load_system_tool_spec_config,
)
from app.tools.runtime.executor import ToolRuntimeExecutor


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


async def _setup_chat_channel(*, channel: str) -> int:
    context = get_default_api_context()
    service = MainAgentSetupService(context)
    try:
        guidance = service.chat_channel_setup_guidance(channel)
    except (MainAgentSetupInvalidError, MainAgentSetupRequiredError, MainAgentModelProfileRequiredError) as exc:
        print(service.startup_guidance(exc))
        return 1

    public_url = await PublicEndpointService(context).get_current_webhook_base_url()
    if public_url:
        channel_path = {
            "discord": "/integrations/conversations/adapters/discord/webhook",
            "telegram": "/integrations/conversations/adapters/telegram/webhook?credential_id=<installation_id>",
            "whatsapp": "/integrations/conversations/adapters/whatsapp/webhook",
        }[channel]
        guidance = (
            f"Current public webhook base URL: {public_url}\n\n"
            f"Current {channel} webhook URL: {public_url}{channel_path}\n\n"
            f"{guidance}"
        )
    print(guidance)
    return 0


async def _public_endpoint_record(args) -> int:
    context = get_default_api_context()
    record = await PublicEndpointService(context).record_webhook_base_url(
        url=args.url,
        provider=args.provider,
        source=args.source,
    )
    payload = record.model_dump(mode="json")
    if args.json:
        _print_json(payload)
    else:
        print(
            "Recorded public endpoint.\n"
            f"  provider: {payload['provider']}\n"
            f"  url: {payload['url']}\n"
            f"  status: {payload['status']}\n"
        )
    return 0


async def _public_endpoint_current(args) -> int:
    context = get_default_api_context()
    url = await PublicEndpointService(context).get_current_webhook_base_url()
    payload = {"url": url}
    if args.json:
        _print_json(payload)
    elif url:
        print(url)
    else:
        print("No active public webhook base URL recorded.")
    return 0


async def _chat_main_agent(*, conversation_id: str, title: str, user_id: str) -> int:
    context = get_default_api_context()
    setup_service = MainAgentSetupService(context)
    conversation_service = ConversationService(context)
    try:
        profile = await setup_service.require_active_main_agent_profile()
    except (MainAgentModelProfileRequiredError, MainAgentSetupRequiredError, MainAgentSetupInvalidError) as exc:
        print(setup_service.startup_guidance(exc))
        return 1

    conversation = await context.conversation_repo.get(conversation_id)
    if conversation is None:
        conversation = await conversation_service.create_conversation(
            {
                "id": conversation_id,
                "title": title,
                "created_by_user_id": user_id,
                "channel_type": "api",
                "metadata": {
                    "source": "cli",
                    "purpose": "direct_main_agent_chat",
                },
            }
        )

    print(
        f"Chatting with main-agent profile: {profile.id}\n"
        "Type `/exit` or `quit` to leave the session.\n"
    )
    while True:
        try:
            user_text = input("you> ").strip()
        except EOFError:
            print()
            break
        if not user_text:
            continue
        if user_text.lower() in {"/exit", "exit", "quit"}:
            break
        response = await conversation_service.post_message(
            conversation.id,
            {
                "message": {
                    "id": f"cli-message-{conversation.id}",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": user_text,
                    "content": {"text": user_text},
                },
                "response_mode": "sync",
            },
        )
        assistant_message = response.get("assistant_message") if isinstance(response, dict) else None
        if isinstance(assistant_message, dict):
            print(f"assistant> {assistant_message.get('plain_text') or ''}")
        approval_request = response.get("approval_request") if isinstance(response, dict) else None
        if isinstance(approval_request, dict):
            summary = approval_request.get("summary") or approval_request.get("id") or "approval requested"
            print(f"approval> {summary}")
    return 0


async def _smoke_test_discord(
        *,
        owner_user_id: str,
        credential_id: str | None,
        discord_user_id: str | None,
        as_json: bool,
) -> int:
    context = get_default_api_context()
    credential_service = CredentialService(context)
    connector_service = ConnectorService(context)
    identity_service = ChannelIdentityMappingService(context)

    resolved_credential_id = credential_id
    if not resolved_credential_id:
        resolution = await credential_service.resolve_connector_credential_for_owner(
            owner_user_id=owner_user_id,
            provider_key="discord",
        )
        if resolution.get("status") != "matched":
            payload = {
                "ok": False,
                "provider": "discord",
                "owner_user_id": owner_user_id,
                "error": resolution.get("error") or "Could not resolve a unique Discord credential.",
                "resolution": resolution,
            }
            if as_json:
                _print_json(payload)
            else:
                print("Discord smoke test failed.")
                print(f"  owner_user_id: {owner_user_id}")
                print(f"  error: {payload['error']}")
            return 1
        credential_summary = resolution.get("credential") or {}
        resolved_credential_id = credential_summary.get("id")

    credential = await credential_service.get_credential_for_owner(resolved_credential_id, owner_user_id)
    if credential is None:
        payload = {
            "ok": False,
            "provider": "discord",
            "owner_user_id": owner_user_id,
            "credential_id": resolved_credential_id,
            "error": "Discord credential not found for owner.",
        }
        if as_json:
            _print_json(payload)
        else:
            print("Discord smoke test failed.")
            print(f"  owner_user_id: {owner_user_id}")
            print(f"  credential_id: {resolved_credential_id}")
            print("  error: Discord credential not found for owner.")
        return 1

    metadata = credential.metadata if isinstance(credential.metadata, dict) else {}
    webhook_public_key = str(metadata.get("webhook_public_key") or "").strip()
    webhook_public_key_valid = False
    if webhook_public_key:
        try:
            webhook_public_key_valid = len(bytes.fromhex(webhook_public_key)) == 32
        except (ValueError, binascii.Error):
            webhook_public_key_valid = False
    owner_discord_mappings = [
        mapping for mapping in await identity_service.list_mappings()
        if mapping.channel_type.value == "discord" and mapping.internal_user_id == owner_user_id
    ]

    trusted_mapping = None
    if discord_user_id:
        trusted_mapping = await identity_service.find_mapping(channel_type="discord", channel_user_id=discord_user_id)

    health = await connector_service.test_credential_for_owner(credential.id, owner_user_id)
    health_ok = bool(isinstance(health, dict) and health.get("ok"))
    mapping_ok = True
    mapping_error = None
    if discord_user_id:
        mapping_ok = bool(
            trusted_mapping is not None
            and trusted_mapping.internal_user_id == owner_user_id
            and trusted_mapping.trusted
        )
        if not mapping_ok:
            mapping_error = (
                "No trusted Discord identity mapping exists for this user id and owner. "
                "Create one before testing protected approvals or mutations."
            )

    checks = {
        "credential_found": True,
        "webhook_public_key_present": bool(webhook_public_key),
        "webhook_public_key_valid": webhook_public_key_valid,
        "connector_health_ok": health_ok,
        "trusted_mapping_ok": mapping_ok,
    }
    ok = all(
        value for key, value in checks.items()
        if key != "trusted_mapping_ok" or discord_user_id is not None
    )
    result = {
        "ok": ok,
        "provider": "discord",
        "owner_user_id": owner_user_id,
        "credential": {
            "id": credential.id,
            "name": credential.name,
            "provider": credential.provider,
            "status": credential.status,
            "secret_ref": credential.secret_ref,
            "metadata": metadata,
        },
        "checks": checks,
        "required_webhook_url": "/integrations/conversations/adapters/discord/webhook",
        "owner_discord_mapping_count": len(owner_discord_mappings),
        "owner_discord_mappings": [
            {
                "id": item.id,
                "channel_user_id": item.channel_user_id,
                "channel_display_name": item.channel_display_name,
                "trusted": item.trusted,
            }
            for item in owner_discord_mappings
        ],
        "health": health,
    }
    if discord_user_id:
        result["discord_user_id"] = discord_user_id
        result["trusted_mapping"] = (
            {
                "id": trusted_mapping.id,
                "channel_user_id": trusted_mapping.channel_user_id,
                "internal_user_id": trusted_mapping.internal_user_id,
                "trusted": trusted_mapping.trusted,
                "channel_display_name": trusted_mapping.channel_display_name,
            }
            if trusted_mapping is not None
            else None
        )
    if not webhook_public_key:
        result["webhook_error"] = (
            "Credential metadata is missing webhook_public_key. "
            "Discord inbound webhook verification is not production-ready without it."
        )
    elif not webhook_public_key_valid:
        result["webhook_error"] = (
            "Credential metadata.webhook_public_key is present but invalid. "
            "Expected the Discord Ed25519 public key as a 64-character hex string."
        )
    if mapping_error:
        result["mapping_error"] = mapping_error

    if as_json:
        _print_json(result)
    else:
        print(f"Discord smoke test: {'PASS' if ok else 'FAIL'}")
        print(f"  owner_user_id: {owner_user_id}")
        print(f"  credential_id: {credential.id}")
        print(f"  provider: {credential.provider}")
        print(f"  status: {credential.status}")
        print(f"  webhook_url: /integrations/conversations/adapters/discord/webhook")
        print(f"  webhook_public_key_present: {'yes' if webhook_public_key else 'no'}")
        print(f"  webhook_public_key_valid: {'yes' if webhook_public_key_valid else 'no'}")
        print(f"  connector_health_ok: {'yes' if health_ok else 'no'}")
        print(f"  owner_discord_mapping_count: {len(owner_discord_mappings)}")
        if discord_user_id:
            print(f"  discord_user_id: {discord_user_id}")
            print(f"  trusted_mapping_ok: {'yes' if mapping_ok else 'no'}")
        if not webhook_public_key:
            print("  action: add metadata.webhook_public_key to the Discord credential")
        elif not webhook_public_key_valid:
            print("  action: replace metadata.webhook_public_key with the Discord application public key hex value")
        if not health_ok:
            print("  action: fix connector health before running live Discord chat tests")
        if mapping_error:
            print(f"  action: {mapping_error}")
    return 0 if ok else 1


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _print_json(payload: object) -> None:
    print(json.dumps(payload, default=_json_default, indent=2, sort_keys=True))


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


def _tool_registry(*, as_json: bool) -> int:
    tools = list_builtin_tool_definitions()
    registry = load_agency_tool_registry_config()
    app_tool_ids = set((registry.get("app_tools") or {}).keys())
    declarative_system_ids = {
        spec["id"]
        for family_specs in load_system_tool_spec_config().values()
        for spec in family_specs
    }
    runtime_system_ids = {
        spec["id"]
        for family_specs in load_system_runtime_tool_spec_config().values()
        for spec in family_specs
    }

    items: list[dict[str, Any]] = []
    for tool in tools:
        if tool.id in app_tool_ids:
            source = "app_tools"
        elif tool.id in declarative_system_ids:
            source = "system_tools"
        elif tool.id in runtime_system_ids:
            source = "system_runtime_tools"
        else:
            source = "unknown"
        items.append(
            {
                "id": tool.id,
                "name": tool.name,
                "display_name": tool.display_name,
                "tool_type": tool.tool_type.value,
                "source": source,
            }
        )

    payload = {
        "count": len(items),
        "yaml_registry_path": "app/tools/config/agency_tools.yaml",
        "source_counts": {
            "app_tools": sum(1 for item in items if item["source"] == "app_tools"),
            "system_tools": sum(1 for item in items if item["source"] == "system_tools"),
            "system_runtime_tools": sum(1 for item in items if item["source"] == "system_runtime_tools"),
            "unknown": sum(1 for item in items if item["source"] == "unknown"),
        },
        "items": items,
    }
    if as_json:
        _print_json(payload)
        return 0

    print(f"{payload['count']} builtin tools")
    print(f"Registry: {payload['yaml_registry_path']}")
    print(
        "Sources: "
        f"app_tools={payload['source_counts']['app_tools']}, "
        f"system_tools={payload['source_counts']['system_tools']}, "
        f"system_runtime_tools={payload['source_counts']['system_runtime_tools']}, "
        f"unknown={payload['source_counts']['unknown']}"
    )
    for item in items:
        print(f"- {item['id']} [{item['source']}] -> {item['display_name']}")
    return 0


def _parse_object_json(raw_payload: str, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON payload: {exc.msg} at line {exc.lineno} column {exc.colno}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return payload


def _parse_json_payload(raw_payload: str) -> dict[str, Any]:
    return _parse_object_json(raw_payload, label="Tool run payload")


def _read_text_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _collect_markdown_files(path: str, *, recursive: bool, pattern: str) -> list[Path]:
    source = Path(path)
    if source.is_file():
        return [source]
    if not source.is_dir():
        raise FileNotFoundError(f"Import path does not exist: {path}")
    files = source.rglob(pattern) if recursive else source.glob(pattern)
    return sorted(item for item in files if item.is_file())


async def _agent_import_preview(args) -> int:
    context = get_default_api_context()
    service = AgentMarkdownImportService(context)
    try:
        proposal = await service.preview_from_request(
            AgentImportPreviewRequest(
                markdown_text=_read_text_file(args.file),
                source_filename=Path(args.file).name,
            )
        )
    except (AgentImportError, ValidationError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    payload = proposal.model_dump(mode="json")
    if args.json:
        _print_json(payload)
        return 0
    print(f"Import preview: {payload['agent']['name']}")
    print(f"  id: {payload['agent']['id']}")
    print(f"  detected_format: {payload['detected_format']}")
    print(f"  suggested_tools: {len(payload['suggested_tool_ids'])}")
    print(f"  suggested_handoffs: {len(payload['suggested_handoff_agent_ids'])}")
    print(f"  warnings: {len(payload['warnings'])}")
    for warning in payload["warnings"]:
        print(f"  warning[{warning['code']}]: {warning['message']}")
    return 0


async def _agent_import_commit(args) -> int:
    context = get_default_api_context()
    service = AgentMarkdownImportService(context)
    try:
        result = await service.commit_from_request(
            AgentImportCommitRequest(
                markdown_text=_read_text_file(args.file),
                source_filename=Path(args.file).name,
                conflict_strategy=args.conflict_strategy,
                approved_tool_ids=args.approve_tool or [],
                approved_handoff_agent_ids=args.approve_handoff or [],
                model_profile_id=args.model_profile_id,
                enabled=args.enabled,
            )
        )
    except (AgentImportError, ValidationError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    payload = result.model_dump(mode="json")
    if args.json:
        _print_json(payload)
        return 0
    print(f"Agent import {payload['status']}: {payload['agent']['id']}")
    print(f"  name: {payload['agent']['name']}")
    print(f"  enabled: {payload['agent']['metadata'].get('enabled')}")
    print(f"  tool_ids: {', '.join(payload['agent']['tool_ids']) or '(none)'}")
    print(f"  handoff_agent_ids: {', '.join(payload['agent']['handoff_agent_ids']) or '(none)'}")
    for warning in payload["warnings"]:
        print(f"  warning[{warning['code']}]: {warning['message']}")
    return 0


async def _agent_import_batch(args) -> int:
    context = get_default_api_context()
    service = AgentMarkdownImportService(context)
    try:
        files = _collect_markdown_files(args.path, recursive=args.recursive, pattern=args.pattern)
        if not files:
            print(f"No Markdown files matched {args.pattern!r} under {args.path}", file=sys.stderr)
            return 1
        preview_payload = AgentImportBatchPreviewRequest(
            items=[
                AgentImportPreviewRequest(
                    markdown_text=file_path.read_text(encoding="utf-8"),
                    source_filename=file_path.name,
                )
                for file_path in files
            ]
        )
        preview = await service.batch_preview_from_request(preview_payload)
        if not args.commit:
            payload = preview.model_dump(mode="json")
            if args.json:
                _print_json(payload)
                return 0
            print(f"Batch import dry run: {len(payload['proposals'])} previewed, {len(payload['errors'])} skipped")
            for proposal in payload["proposals"]:
                warnings = len(proposal["warnings"])
                conflicts = len(proposal["conflicts"])
                print(
                    f"  {proposal['agent']['name']}\t"
                    f"{proposal['detected_format']}\t"
                    f"warnings={warnings}\t"
                    f"conflicts={conflicts}"
                )
            for error in payload["errors"]:
                print(f"  skipped[{error['index']}]: {error['message']}")
            return 0 if not preview.errors else 1

        result = await service.batch_commit_from_request(
            AgentImportBatchCommitRequest(
                items=[
                    AgentImportCommitRequest(
                        proposal=proposal,
                        conflict_strategy=args.conflict_strategy,
                        approved_tool_ids=args.approve_tool or [],
                        approved_handoff_agent_ids=args.approve_handoff or [],
                        model_profile_id=args.model_profile_id,
                        enabled=args.enabled,
                    )
                    for proposal in preview.proposals
                ]
            )
        )
        result.errors = [*preview.errors, *result.errors]
    except (AgentImportError, ValidationError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    payload = result.model_dump(mode="json")
    if args.json:
        _print_json(payload)
        return 0
    print(f"Batch import commit: {len(payload['results'])} saved, {len(payload['errors'])} failed")
    for item in payload["results"]:
        print(f"  {item['status']}: {item['agent']['id']}\t{item['agent']['name']}")
        for warning in item["warnings"]:
            print(f"    warning[{warning['code']}]: {warning['message']}")
    for error in payload["errors"]:
        label = error.get("source_filename") or error.get("source_url") or error["index"]
        print(f"  failed[{label}]: {error['message']}")
    return 0 if not result.errors else 1


def _connector_payload(
        *,
        name: str | None = None,
        workflow_id: str | None = None,
        metadata_json: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if name:
        payload["name"] = name
    if workflow_id:
        payload["workflow_id"] = workflow_id
    if metadata_json:
        payload["metadata"] = _parse_object_json(metadata_json, label="Connector metadata")
    return payload


async def _connector_setup(args) -> int:
    context = get_default_api_context()
    service = ConnectorInstallationService(context)
    try:
        session = await service.create_setup_session(
            provider_key=args.provider,
            payload=_connector_payload(
                name=args.name,
                workflow_id=args.workflow_id,
                metadata_json=args.metadata_json,
            ),
            owner_user_id=args.owner_user_id,
        )
    except (LookupError, ValidationError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    payload = session.model_dump(mode="json")
    if args.json:
        _print_json(payload)
    else:
        print(f"Connector setup session: {payload['installation']['id']}")
        print(f"  provider: {payload['installation']['provider']}")
        print(f"  status: {payload['installation']['status']}")
        print(f"  device_code: {payload['device_code']}")
        print(f"  setup_url: {payload['setup_url']}")
        print(f"  onecli_credential_ref: {payload['onecli_credential_ref']}")
    return 0


async def _connector_list(args) -> int:
    context = get_default_api_context()
    service = ConnectorInstallationService(context)
    installations = await service.list_for_owner(args.owner_user_id)
    payload = {"items": [item.model_dump(mode="json") for item in installations]}
    if args.json:
        _print_json(payload)
    else:
        print(f"{len(installations)} connector installation(s)")
        for item in payload["items"]:
            print(f"{item['id']}\t{item['provider']}\t{item['status']}\t{item['name']}")
    return 0


async def _connector_status(args) -> int:
    context = get_default_api_context()
    service = ConnectorInstallationService(context)
    installation = await service.get_for_owner(args.installation_id, args.owner_user_id)
    if installation is None:
        print("Connector installation not found", file=sys.stderr)
        return 1
    payload = installation.model_dump(mode="json")
    if args.json:
        _print_json(payload)
    else:
        print(f"Connector installation: {payload['id']}")
        print(f"  provider: {payload['provider']}")
        print(f"  status: {payload['status']}")
        print(f"  name: {payload['name']}")
        print(f"  onecli_credential_ref: {payload['onecli_credential_ref']}")
    return 0


async def _connector_complete(args) -> int:
    context = get_default_api_context()
    service = ConnectorInstallationService(context)
    try:
        installation = await service.complete_for_owner(
            installation_id=args.installation_id,
            owner_user_id=args.owner_user_id,
            payload=_connector_payload(
                metadata_json=args.metadata_json,
            ),
        )
    except (ValidationError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if installation is None:
        print("Connector installation not found", file=sys.stderr)
        return 1
    payload = installation.model_dump(mode="json")
    if args.json:
        _print_json(payload)
    else:
        print(f"Connector installation completed: {payload['id']}")
        print(f"  status: {payload['status']}")
    return 0


async def _connector_rotate(args) -> int:
    context = get_default_api_context()
    service = ConnectorInstallationService(context)
    try:
        session = await service.rotate_for_owner(
            installation_id=args.installation_id,
            owner_user_id=args.owner_user_id,
            payload=_connector_payload(
                metadata_json=args.metadata_json,
            ),
        )
    except (ValidationError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if session is None:
        print("Connector installation not found", file=sys.stderr)
        return 1
    payload = session.model_dump(mode="json")
    if args.json:
        _print_json(payload)
    else:
        print(f"Connector rotation session: {payload['installation']['id']}")
        print(f"  status: {payload['installation']['status']}")
        print(f"  device_code: {payload['device_code']}")
        print(f"  setup_url: {payload['setup_url']}")
    return 0


async def _connector_revoke(args) -> int:
    context = get_default_api_context()
    service = ConnectorInstallationService(context)
    installation = await service.revoke_for_owner(args.installation_id, args.owner_user_id)
    if installation is None:
        print("Connector installation not found", file=sys.stderr)
        return 1
    payload = installation.model_dump(mode="json")
    if args.json:
        _print_json(payload)
    else:
        print(f"Connector installation revoked: {payload['id']}")
        print(f"  status: {payload['status']}")
    return 0


async def _graph_projection_status(args) -> int:
    context = get_default_api_context()
    summary = await context.graph_projection_event_repo.status_summary()
    payload = {
        "enabled": get_settings().graph_projection_enabled,
        **summary,
    }
    if args.json:
        _print_json(payload)
    else:
        print(f"Graph projection enabled: {payload['enabled']}")
        print(f"Pending: {payload['pending_count']}")
        print(f"Projected: {payload['projected_count']}")
        print(f"Failed: {payload['failed_count']}")
        print(f"Health: {payload.get('health_status')}")
        print(f"Projection lag seconds: {payload.get('projection_lag_seconds')}")
        print(f"Last projected at: {payload['last_projected_at']}")
        print(f"Last projected execution event at: {payload.get('last_projected_execution_event_at')}")
        print(f"Last projected memory event at: {payload.get('last_projected_memory_event_at')}")
        print(f"Oldest pending at: {payload['oldest_pending_at']}")
        if payload.get("last_error"):
            print(f"Last error: {payload['last_error']}")
    return 0


async def _graph_projection_replay(args) -> int:
    context = get_default_api_context()
    event_ids = args.event_id or None
    if args.failed_only:
        failed_events = await context.graph_projection_event_repo.list_events(status="failed", limit=args.limit)
        event_ids = [event.event_id for event in failed_events]
    worker = GraphProjectionWorker(context.graph_projection_event_repo, batch_size=args.limit)
    result = await worker.replay(event_ids=event_ids, run=args.run)
    payload = {
        "reset": True,
        "run": args.run,
        "event_ids": event_ids,
        "processed": result.processed,
        "failed": result.failed,
        "checkpoint_event_id": result.checkpoint_event_id,
        "errors": result.errors,
    }
    if args.json:
        _print_json(payload)
    else:
        print("Graph projection replay reset complete.")
        print(f"Run projector: {payload['run']}")
        print(f"Processed: {payload['processed']}")
        print(f"Failed: {payload['failed']}")
        print(f"Checkpoint: {payload['checkpoint_event_id']}")
    return 0


async def _graph_projection_backfill(args) -> int:
    context = get_default_api_context()
    result = await GraphProjectionBackfillService(context).backfill(
        domains=args.domain,
        limit=args.limit,
    )
    payload = result.to_dict()
    if args.json:
        _print_json(payload)
    else:
        print("Graph projection backfill enqueue complete.")
        print(f"Scanned: {payload['scanned']}")
        print(f"Enqueued: {payload['enqueued']}")
        print(f"Skipped: {payload['skipped']}")
        for domain, counts in payload["domains"].items():
            print(
                f"{domain}: scanned={counts['scanned']} "
                f"enqueued={counts['enqueued']} skipped={counts['skipped']}"
            )
        for error in payload["errors"]:
            print(f"warning: {error}")
    return 0 if not result.errors else 1


async def _graph_projection_project_neo4j(args) -> int:
    settings = get_settings()
    if not settings.neo4j_enabled and not args.force:
        print("Neo4j projection is disabled. Set NEO4J_ENABLED=true or pass --force.", file=sys.stderr)
        return 1
    context = get_default_api_context()
    driver = create_neo4j_driver(settings)
    projector = Neo4jGraphProjector(driver, config=Neo4jProjectionConfig(database=settings.neo4j_database))
    try:
        if args.ensure_schema:
            await projector.ensure_schema()
        result = await projector.project_pending(context.graph_projection_event_repo, limit=args.limit)
    finally:
        await projector.close()
    payload = {
        "processed": result.processed,
        "failed": result.failed,
        "checkpoint_event_id": result.checkpoint_event_id,
        "errors": result.errors,
    }
    if args.json:
        _print_json(payload)
    else:
        print(f"Processed: {payload['processed']}")
        print(f"Failed: {payload['failed']}")
        print(f"Checkpoint: {payload['checkpoint_event_id']}")
    return 0 if result.failed == 0 else 1


async def _graph_projection_project_neo4j_loop(args) -> int:
    settings = get_settings()
    if not settings.neo4j_enabled and not args.force:
        print("Neo4j projection is disabled. Set NEO4J_ENABLED=true or pass --force.", file=sys.stderr)
        return 1

    context = get_default_api_context()
    driver = create_neo4j_driver(settings)
    projector = Neo4jGraphProjector(driver, config=Neo4jProjectionConfig(database=settings.neo4j_database))
    schema_ready = not args.ensure_schema
    iteration = 0
    exit_code = 0
    try:
        while True:
            iteration += 1
            payload: dict[str, Any]
            try:
                # Keep the long-running projector resilient to startup ordering and brief Neo4j outages.
                if not schema_ready:
                    await projector.ensure_schema()
                    schema_ready = True
                result = await projector.project_pending(context.graph_projection_event_repo, limit=args.limit)
                payload = {
                    "iteration": iteration,
                    "processed": result.processed,
                    "failed": result.failed,
                    "checkpoint_event_id": result.checkpoint_event_id,
                    "errors": result.errors,
                }
                if result.failed:
                    exit_code = 1
            except Exception as exc:  # pragma: no cover - defensive runtime loop guard
                payload = {
                    "iteration": iteration,
                    "processed": 0,
                    "failed": 1,
                    "checkpoint_event_id": None,
                    "errors": [str(exc)],
                }
                exit_code = 1
                schema_ready = not args.ensure_schema
                if args.stop_on_error:
                    if args.json:
                        _print_json(payload)
                    else:
                        print(f"Projection loop error: {exc}", file=sys.stderr)
                    return 1

            if args.json:
                _print_json(payload)
            else:
                print(
                    "Projection loop iteration "
                    f"{payload['iteration']}: processed={payload['processed']} "
                    f"failed={payload['failed']} checkpoint={payload['checkpoint_event_id']}"
                )
                for error in payload["errors"]:
                    print(f"warning: {error}")

            if args.max_iterations and iteration >= args.max_iterations:
                return exit_code
            await asyncio.sleep(args.interval_seconds)
    except KeyboardInterrupt:
        return exit_code
    finally:
        await projector.close()


async def _graph_projection_rebuild_neo4j(args) -> int:
    if args.clear and not args.confirm_clear:
        print("Refusing to clear Neo4j projection without --confirm-clear.", file=sys.stderr)
        return 1

    context = get_default_api_context()
    settings = get_settings()
    if args.dry_run:
        projector = Neo4jGraphProjector(driver=None, config=Neo4jProjectionConfig(database=settings.neo4j_database))
        rebuilder = Neo4jGraphRebuilder(context.graph_projection_event_repo, projector, batch_size=args.batch_size)
        result = await rebuilder.rebuild(dry_run=True)
    else:
        if not settings.neo4j_enabled and not args.force:
            print("Neo4j rebuild is disabled. Set NEO4J_ENABLED=true or pass --force.", file=sys.stderr)
            return 1
        driver = create_neo4j_driver(settings)
        projector = Neo4jGraphProjector(driver, config=Neo4jProjectionConfig(database=settings.neo4j_database))
        rebuilder = Neo4jGraphRebuilder(context.graph_projection_event_repo, projector, batch_size=args.batch_size)
        try:
            result = await rebuilder.rebuild(
                clear=args.clear,
                ensure_schema=not args.skip_schema,
                dry_run=False,
            )
        finally:
            await projector.close()

    payload = result.to_dict()
    if args.json:
        _print_json(payload)
    else:
        print("Graph projection Neo4j rebuild " + ("dry run." if result.dry_run else "complete."))
        print(f"Dry run: {payload['dry_run']}")
        print(f"Cleared graph: {payload['cleared']}")
        print(f"Reset events: {payload['reset_events']}")
        print(f"Processed: {payload['processed']}")
        print(f"Failed: {payload['failed']}")
        print(f"Checkpoint: {payload['checkpoint_event_id']}")
    return 0 if result.failed == 0 else 1


async def _graph_projection_parity(args) -> int:
    settings = get_settings()
    if not settings.neo4j_enabled and not args.force:
        print("Neo4j parity checks are disabled. Set NEO4J_ENABLED=true or pass --force.", file=sys.stderr)
        return 1

    context = get_default_api_context()
    driver = create_neo4j_driver(settings)
    checker = Neo4jGraphParityChecker(driver, config=Neo4jProjectionConfig(database=settings.neo4j_database))
    try:
        result = await checker.check(context.graph_projection_event_repo, event_limit=args.event_limit)
    finally:
        await checker.close()

    payload = result.to_dict()
    if args.json:
        _print_json(payload)
    else:
        print(f"Graph projection parity ok: {payload['ok']}")
        print(f"Checked projected events: {payload['checked_events']}")
        print(f"Truncated: {payload['truncated']}")
        print(f"Pending outbox events: {payload['outbox_status'].get('pending_count')}")
        print(f"Failed outbox events: {payload['outbox_status'].get('failed_count')}")
        print(f"Projection lag seconds: {payload['outbox_status'].get('projection_lag_seconds')}")
        for item in payload["items"]:
            print(
                f"{item['kind']} {item['name']}: "
                f"expected={item['expected']} actual={item['actual']} delta={item['delta']} ok={item['ok']}"
            )
        nonzero_node_counts = {key: value for key, value in payload["node_counts_by_type"].items() if value}
        nonzero_edge_counts = {key: value for key, value in payload["edge_counts_by_type"].items() if value}
        if nonzero_node_counts:
            print(f"Active node counts by type: {nonzero_node_counts}")
        if nonzero_edge_counts:
            print(f"Active edge counts by type: {nonzero_edge_counts}")
        for error in payload["errors"]:
            print(f"warning: {error}")
    return 0 if result.ok else 1


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
        help="Run the operator-style main-agent setup flow for recovery or headless bootstrap.",
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
    chat_channel_parser = subparsers.add_parser(
        "setup-chat-channel",
        help="Print the backend setup checklist for a chat channel integration.",
    )
    chat_channel_parser.add_argument(
        "channel",
        choices=["discord", "telegram", "whatsapp"],
        help="Chat channel to prepare.",
    )
    chat_parser = subparsers.add_parser(
        "chat-main-agent",
        help="Open an interactive terminal chat with the active main agent.",
    )
    chat_parser.add_argument(
        "--conversation-id",
        default="main-agent-cli",
        help="Conversation id to reuse across CLI chat sessions.",
    )
    chat_parser.add_argument(
        "--title",
        default="Main Agent CLI",
        help="Conversation title to create when the CLI session starts.",
    )
    chat_parser.add_argument(
        "--user-id",
        default="cli-user",
        help="Owning user id to assign to the CLI chat conversation.",
    )
    discord_smoke_parser = subparsers.add_parser(
        "smoke-test-discord",
        help="Check whether a Discord connector installation is ready for live backend chat.",
    )
    discord_smoke_parser.add_argument(
        "--owner-user-id",
        required=True,
        help="Internal Agency user id that owns the Discord credential.",
    )
    discord_smoke_parser.add_argument(
        "--credential-id",
        help="Discord credential id. Omit to auto-resolve the owner's active Discord connector.",
    )
    discord_smoke_parser.add_argument(
        "--discord-user-id",
        help="Discord user id to verify against trusted channel identity mappings.",
    )
    discord_smoke_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
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

    tool_registry_parser = tool_subparsers.add_parser(
        "registry",
        help="Show the assembled builtin tool registry and YAML source classification.",
    )
    tool_registry_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

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

    agent_parser = subparsers.add_parser(
        "agent",
        help="Manage agent definitions.",
    )
    agent_subparsers = agent_parser.add_subparsers(dest="agent_command", required=True)

    agent_import_preview_parser = agent_subparsers.add_parser(
        "import-preview",
        help="Preview importing a Markdown-authored agent without saving it.",
    )
    agent_import_preview_parser.add_argument("file", help="Path to a Markdown agent file.")
    agent_import_preview_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    agent_import_commit_parser = agent_subparsers.add_parser(
        "import-commit",
        help="Import a Markdown-authored agent into the backend catalog.",
    )
    agent_import_commit_parser.add_argument("file", help="Path to a Markdown agent file.")
    agent_import_commit_parser.add_argument(
        "--conflict-strategy",
        choices=["create_only", "update_existing", "duplicate_as_new"],
        default="create_only",
        help="How to handle an existing agent with the same id or name.",
    )
    agent_import_commit_parser.add_argument(
        "--approve-tool",
        action="append",
        help="Explicitly approve assigning an imported tool id. Repeat for multiple tools.",
    )
    agent_import_commit_parser.add_argument(
        "--approve-handoff",
        action="append",
        help="Explicitly approve assigning an imported handoff agent id or name. Repeat for multiple agents.",
    )
    agent_import_commit_parser.add_argument("--model-profile-id", help="Model profile id to assign.")
    agent_import_commit_parser.add_argument(
        "--enabled",
        action="store_true",
        default=None,
        help="Enable the imported agent immediately. Defaults to disabled.",
    )
    agent_import_commit_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    agent_import_batch_parser = agent_subparsers.add_parser(
        "import-batch",
        help="Preview or commit Markdown-authored agents from a file or folder. Dry-run by default.",
    )
    agent_import_batch_parser.add_argument("path", help="Markdown file or folder to import.")
    agent_import_batch_parser.add_argument(
        "--pattern",
        default="*.md",
        help="Glob pattern used when path is a folder. Defaults to *.md.",
    )
    agent_import_batch_parser.add_argument(
        "--recursive",
        action="store_true",
        help="Scan folders recursively instead of only the immediate directory.",
    )
    agent_import_batch_parser.add_argument(
        "--commit",
        action="store_true",
        help="Save previewed agents. Without this flag the command only previews.",
    )
    agent_import_batch_parser.add_argument(
        "--conflict-strategy",
        choices=["create_only", "update_existing", "duplicate_as_new"],
        default="create_only",
        help="How to handle existing agents with the same id or name during commit.",
    )
    agent_import_batch_parser.add_argument(
        "--approve-tool",
        action="append",
        help="Explicitly approve assigning an imported tool id to every committed agent. Repeat for multiple tools.",
    )
    agent_import_batch_parser.add_argument(
        "--approve-handoff",
        action="append",
        help="Explicitly approve assigning an imported handoff id or name to every committed agent. Repeat for multiple agents.",
    )
    agent_import_batch_parser.add_argument("--model-profile-id",
                                           help="Model profile id to assign to every committed agent.")
    agent_import_batch_parser.add_argument(
        "--enabled",
        action="store_true",
        default=None,
        help="Enable imported agents immediately when committing. Defaults to disabled.",
    )
    agent_import_batch_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    connector_parser = subparsers.add_parser(
        "connector",
        help="Manage backend-owned OneCLI connector setup sessions.",
    )
    connector_subparsers = connector_parser.add_subparsers(dest="connector_command", required=True)

    connector_setup_parser = connector_subparsers.add_parser(
        "setup",
        help="Create or reuse a OneCLI setup session for a connector provider.",
    )
    connector_setup_parser.add_argument("provider", help="Connector provider key, such as telegram or discord.")
    connector_setup_parser.add_argument("--owner-user-id", required=True, help="Agency owner user id.")
    connector_setup_parser.add_argument("--name", help="Display name for the connector installation.")
    connector_setup_parser.add_argument("--workflow-id", help="Optional workflow-scoped connector owner.")
    connector_setup_parser.add_argument("--metadata-json", help="JSON object with non-secret connector metadata.")
    connector_setup_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    connector_list_parser = connector_subparsers.add_parser(
        "list",
        help="List connector installations for an Agency user.",
    )
    connector_list_parser.add_argument("--owner-user-id", required=True, help="Agency owner user id.")
    connector_list_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    connector_status_parser = connector_subparsers.add_parser(
        "status",
        help="Read one connector installation status.",
    )
    connector_status_parser.add_argument("installation_id", help="Connector installation id.")
    connector_status_parser.add_argument("--owner-user-id", required=True, help="Agency owner user id.")
    connector_status_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    connector_complete_parser = connector_subparsers.add_parser(
        "complete",
        help="Verify the session-specific OneCLI resource and activate the installation.",
    )
    connector_complete_parser.add_argument("installation_id", help="Connector installation id.")
    connector_complete_parser.add_argument("--owner-user-id", required=True, help="Agency owner user id.")
    connector_complete_parser.add_argument("--metadata-json", help="JSON object with non-secret connector metadata.")
    connector_complete_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    connector_rotate_parser = connector_subparsers.add_parser(
        "rotate",
        help="Create a OneCLI rotation setup session for an existing connector installation.",
    )
    connector_rotate_parser.add_argument("installation_id", help="Connector installation id.")
    connector_rotate_parser.add_argument("--owner-user-id", required=True, help="Agency owner user id.")
    connector_rotate_parser.add_argument("--metadata-json", help="JSON object with non-secret connector metadata.")
    connector_rotate_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    connector_revoke_parser = connector_subparsers.add_parser(
        "revoke",
        help="Revoke a connector installation.",
    )
    connector_revoke_parser.add_argument("installation_id", help="Connector installation id.")
    connector_revoke_parser.add_argument("--owner-user-id", required=True, help="Agency owner user id.")
    connector_revoke_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    public_endpoint_parser = subparsers.add_parser(
        "public-endpoint",
        help="Persist or inspect the current launcher-discovered public webhook base URL.",
    )
    public_endpoint_subparsers = public_endpoint_parser.add_subparsers(
        dest="public_endpoint_command",
        required=True,
    )

    public_endpoint_record_parser = public_endpoint_subparsers.add_parser(
        "record",
        help="Record the current public webhook base URL.",
    )
    public_endpoint_record_parser.add_argument("--url", required=True,
                                               help="Absolute https URL published by the launcher.")
    public_endpoint_record_parser.add_argument(
        "--provider",
        default="cloudflare",
        help="Tunnel provider name, for example cloudflare or ngrok.",
    )
    public_endpoint_record_parser.add_argument(
        "--source",
        default="launcher",
        help="Origin of the recorded endpoint.",
    )
    public_endpoint_record_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    public_endpoint_current_parser = public_endpoint_subparsers.add_parser(
        "current",
        help="Print the current active public webhook base URL.",
    )
    public_endpoint_current_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    graph_projection_parser = subparsers.add_parser(
        "graph-projection",
        help="Inspect and replay graph projection outbox events.",
    )
    graph_projection_subparsers = graph_projection_parser.add_subparsers(
        dest="graph_projection_command",
        required=True,
    )

    graph_projection_status_parser = graph_projection_subparsers.add_parser(
        "status",
        help="Show graph projection outbox status.",
    )
    graph_projection_status_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    graph_projection_replay_parser = graph_projection_subparsers.add_parser(
        "replay",
        help="Reset graph projection events to pending and optionally run the no-op projector.",
    )
    graph_projection_replay_parser.add_argument("--event-id", action="append", help="Specific event id to reset.")
    graph_projection_replay_parser.add_argument("--failed-only", action="store_true", help="Reset failed events only.")
    graph_projection_replay_parser.add_argument("--run", action="store_true",
                                                help="Run the no-op projector after reset.")
    graph_projection_replay_parser.add_argument("--limit", type=int, default=100, help="Maximum events to process.")
    graph_projection_replay_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    graph_projection_backfill_parser = graph_projection_subparsers.add_parser(
        "backfill",
        help="Regenerate graph projection outbox events from source records.",
    )
    graph_projection_backfill_parser.add_argument(
        "--domain",
        action="append",
        choices=[
            "all",
            "workflows",
            "workflow_memory_links",
            "executions",
            "memories",
            "documents",
            "source_intelligence_graph_hints",
            "source-intelligence-graph-hints",
            "graph-hints",
        ],
        help="Domain to backfill. Repeat to include multiple domains. Defaults to all.",
    )
    graph_projection_backfill_parser.add_argument("--limit", type=int, default=1000, help="Maximum records per domain.")
    graph_projection_backfill_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    graph_projection_neo4j_parser = graph_projection_subparsers.add_parser(
        "project-neo4j",
        help="Project pending graph events into Neo4j.",
    )
    graph_projection_neo4j_parser.add_argument("--limit", type=int, default=100, help="Maximum events to project.")
    graph_projection_neo4j_parser.add_argument(
        "--ensure-schema",
        action="store_true",
        help="Create Neo4j constraints before projecting.",
    )
    graph_projection_neo4j_parser.add_argument(
        "--force",
        action="store_true",
        help="Run even when NEO4J_ENABLED is false.",
    )
    graph_projection_neo4j_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    graph_projection_neo4j_loop_parser = graph_projection_subparsers.add_parser(
        "project-neo4j-loop",
        help="Continuously project pending graph events into Neo4j.",
    )
    graph_projection_neo4j_loop_parser.add_argument("--limit", type=int, default=100, help="Maximum events per pass.")
    graph_projection_neo4j_loop_parser.add_argument(
        "--interval-seconds",
        type=float,
        default=5.0,
        help="Sleep interval between projection passes.",
    )
    graph_projection_neo4j_loop_parser.add_argument(
        "--ensure-schema",
        action="store_true",
        help="Create Neo4j constraints before the first successful projection pass.",
    )
    graph_projection_neo4j_loop_parser.add_argument(
        "--force",
        action="store_true",
        help="Run even when NEO4J_ENABLED is false.",
    )
    graph_projection_neo4j_loop_parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Exit when a projection pass raises an unexpected error.",
    )
    graph_projection_neo4j_loop_parser.add_argument(
        "--max-iterations",
        type=int,
        default=0,
        help="Stop after this many passes. Zero means run forever.",
    )
    graph_projection_neo4j_loop_parser.add_argument("--json", action="store_true",
                                                    help="Emit one JSON object per pass.")

    graph_projection_rebuild_parser = graph_projection_subparsers.add_parser(
        "rebuild-neo4j",
        help="Reset and replay graph projection events into Neo4j.",
    )
    graph_projection_rebuild_parser.add_argument("--batch-size", type=int, default=100,
                                                 help="Events to project per batch.")
    graph_projection_rebuild_parser.add_argument(
        "--clear",
        action="store_true",
        help="Delete projected Agency graph labels from Neo4j before replaying.",
    )
    graph_projection_rebuild_parser.add_argument(
        "--confirm-clear",
        action="store_true",
        help="Required with --clear to confirm destructive Neo4j graph cleanup.",
    )
    graph_projection_rebuild_parser.add_argument(
        "--skip-schema",
        action="store_true",
        help="Skip creating Neo4j constraints before replaying.",
    )
    graph_projection_rebuild_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report outbox event count and projected labels without mutating Neo4j or Postgres.",
    )
    graph_projection_rebuild_parser.add_argument(
        "--force",
        action="store_true",
        help="Run even when NEO4J_ENABLED is false.",
    )
    graph_projection_rebuild_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    graph_projection_parity_parser = graph_projection_subparsers.add_parser(
        "parity",
        help="Compare projected outbox counts with Neo4j active graph counts.",
    )
    graph_projection_parity_parser.add_argument(
        "--event-limit",
        type=int,
        default=10000,
        help="Maximum projected outbox events to scan.",
    )
    graph_projection_parity_parser.add_argument(
        "--force",
        action="store_true",
        help="Run even when NEO4J_ENABLED is false.",
    )
    graph_projection_parity_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
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
    if args.command == "setup-chat-channel":
        return asyncio.run(_setup_chat_channel(channel=args.channel))
    if args.command == "chat-main-agent":
        return asyncio.run(
            _chat_main_agent(
                conversation_id=args.conversation_id,
                title=args.title,
                user_id=args.user_id,
            )
        )
    if args.command == "smoke-test-discord":
        return asyncio.run(
            _smoke_test_discord(
                owner_user_id=args.owner_user_id,
                credential_id=args.credential_id,
                discord_user_id=args.discord_user_id,
                as_json=args.json,
            )
        )
    if args.command == "tool":
        if args.tool_command == "list":
            return _tool_list(as_json=args.json)
        if args.tool_command == "describe":
            return _tool_describe(args.tool, as_json=args.json)
        if args.tool_command == "schema":
            return _tool_schema(args.tool, which=args.which, as_json=args.json)
        if args.tool_command == "registry":
            return _tool_registry(as_json=args.json)
        if args.tool_command == "run":
            return _tool_run(args.tool, raw_payload=args.json, actor=args.actor, as_json=args.output_json)
        parser.error(f"Unknown tool command: {args.tool_command}")
    if args.command == "agent":
        if args.agent_command == "import-preview":
            return asyncio.run(_agent_import_preview(args))
        if args.agent_command == "import-commit":
            return asyncio.run(_agent_import_commit(args))
        if args.agent_command == "import-batch":
            return asyncio.run(_agent_import_batch(args))
        parser.error(f"Unknown agent command: {args.agent_command}")
    if args.command == "connector":
        if args.connector_command == "setup":
            return asyncio.run(_connector_setup(args))
        if args.connector_command == "list":
            return asyncio.run(_connector_list(args))
        if args.connector_command == "status":
            return asyncio.run(_connector_status(args))
        if args.connector_command == "complete":
            return asyncio.run(_connector_complete(args))
        if args.connector_command == "rotate":
            return asyncio.run(_connector_rotate(args))
        if args.connector_command == "revoke":
            return asyncio.run(_connector_revoke(args))
        parser.error(f"Unknown connector command: {args.connector_command}")
    if args.command == "public-endpoint":
        if args.public_endpoint_command == "record":
            return asyncio.run(_public_endpoint_record(args))
        if args.public_endpoint_command == "current":
            return asyncio.run(_public_endpoint_current(args))
        parser.error(f"Unknown public-endpoint command: {args.public_endpoint_command}")
    if args.command == "graph-projection":
        if args.graph_projection_command == "status":
            return asyncio.run(_graph_projection_status(args))
        if args.graph_projection_command == "replay":
            return asyncio.run(_graph_projection_replay(args))
        if args.graph_projection_command == "backfill":
            return asyncio.run(_graph_projection_backfill(args))
        if args.graph_projection_command == "project-neo4j":
            return asyncio.run(_graph_projection_project_neo4j(args))
        if args.graph_projection_command == "project-neo4j-loop":
            return asyncio.run(_graph_projection_project_neo4j_loop(args))
        if args.graph_projection_command == "rebuild-neo4j":
            return asyncio.run(_graph_projection_rebuild_neo4j(args))
        if args.graph_projection_command == "parity":
            return asyncio.run(_graph_projection_parity(args))
        parser.error(f"Unknown graph projection command: {args.graph_projection_command}")
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
