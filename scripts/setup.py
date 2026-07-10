#!/usr/bin/env python3
"""Idempotent setup entrypoint for Agency-managed agents.

The public docs keep the canonical prompts in markdown files under ``docs/``.
This script extracts those prompt fences and persists the matching database
records for the main, Coder, Embedding, and Evaluation agents. It is safe to run
multiple times: existing records are updated in place while user-managed fields
such as extra tool assignments are preserved where the setup contract allows it.
Local-first onboarding now lives primarily in ``/setup`` and ``local-onboarding``;
this module remains the explicit maintenance and headless bootstrap surface.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path

try:
    from scripts._bootstrap import bootstrap_repo
except ModuleNotFoundError:
    from _bootstrap import bootstrap_repo

REPO_ROOT = bootstrap_repo(__file__, reexec=__name__ == "__main__")

from app.api.context import ApiContext, get_default_api_context
from app.core.config import get_settings
from app.domain import (
    AgentDefinition,
    FrameworkHints,
    GraphContextSettings,
    MemorySettings,
    ModelProfileDefinition,
    ModelProviderDefinition,
    ModelProviderType,
)
from app.services.agent_tools import (
    AgentToolResolver,
    SYSTEM_COMMAND_RUN_TOOL_ID,
    SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID,
    SYSTEM_EXECUTION_EVENTS_TOOL_ID,
    SYSTEM_EXECUTION_GET_TOOL_ID,
    SYSTEM_EXECUTION_LIST_TOOL_ID,
    SYSTEM_GRAPH_CONTEXT_TOOL_ID,
    SYSTEM_WORKFLOW_GET_TOOL_ID,
    SYSTEM_WORKFLOW_LIST_TOOL_ID,
)
from app.services.local_auth import LocalAuthBootstrapUnavailableError, LocalAuthService
from app.services.main_agent_setup import (
    MainAgentModelProfileRequiredError,
    MainAgentSetupInvalidError,
    MainAgentSetupRequiredError,
    MainAgentSetupService,
)
from app.services.setup_onboarding import SetupOnboardingService


MAIN_AGENT_PROMPT_DOC = REPO_ROOT / "docs" / "main-agent.md"
CODER_PROMPT_DOC = REPO_ROOT / "docs" / "coding-agent.md"
EMBEDDING_PROMPT_DOC = REPO_ROOT / "docs" / "embedding-agent.md"
EVALUATION_PROMPT_DOC = REPO_ROOT / "docs" / "evaluation-agent.md"
MANAGED_BY = "scripts/setup.py"

CODER_DEFAULT_NAME = "Coder"
CODER_DEFAULT_ROLE = "Senior Software Engineer"

DEFAULT_NAME = "Embedding"
DEFAULT_ROLE = "Memory Embedding Worker"
DEFAULT_PROVIDER_ID = "ollama"
DEFAULT_PROVIDER_NAME = "Ollama"
DEFAULT_MODEL_PROFILE_ID = "embedding-nemotron-nano"
DEFAULT_MODEL_PROFILE_NAME = "Nemotron Nano Embeddings"
DEFAULT_MODEL = "huihui_ai/nemotron-v1-abliterated:8b-llama-3.1-nano"

EVALUATION_DEFAULT_NAME = "Evaluation"
EVALUATION_DEFAULT_ROLE = "Evaluation Judge"
READ_ONLY_TOOL_IDS = [
    SYSTEM_EXECUTION_LIST_TOOL_ID,
    SYSTEM_EXECUTION_GET_TOOL_ID,
    SYSTEM_EXECUTION_EVENTS_TOOL_ID,
    SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID,
    SYSTEM_WORKFLOW_GET_TOOL_ID,
    SYSTEM_WORKFLOW_LIST_TOOL_ID,
]


async def _ensure_graph_context_tool_if_enabled(context: ApiContext) -> bool:
    if not get_settings().agency_graph_context_tools_enabled:
        return False
    resolver = AgentToolResolver(context)
    await resolver.ensure_graph_system_tools(can_read_graph_context=True)
    return await context.tool_repo.get(SYSTEM_GRAPH_CONTEXT_TOOL_ID) is not None


def _append_tool(tool_ids: list[str], tool_id: str) -> list[str]:
    return [*tool_ids, tool_id] if tool_id not in tool_ids else tool_ids


@dataclass(slots=True)
class EmbeddingAgentSetupResult:
    """Objects created or updated by the Embedding agent setup path."""

    provider: ModelProviderDefinition
    model_profile: ModelProfileDefinition
    agent: AgentDefinition


@dataclass(slots=True)
class EvaluationAgentSetupResult:
    """Objects selected or updated by the Evaluation agent setup path."""

    agent: AgentDefinition
    model_profile: ModelProfileDefinition | None
    reserved_model_profile_ids: set[str]


@dataclass(slots=True)
class AgentSetupResult:
    """Summary for the aggregate setup command used by launch scripts."""

    main_agent_profile_id: str | None
    coder_agent: AgentDefinition | None
    embedding: EmbeddingAgentSetupResult
    evaluation: EvaluationAgentSetupResult


@dataclass(slots=True)
class RecommendedAgentsSyncResult:
    """Summary for syncing the non-primary built-in agents after onboarding."""

    coder_agent: AgentDefinition
    embedding: EmbeddingAgentSetupResult
    evaluation: EvaluationAgentSetupResult


@dataclass(slots=True)
class LocalOnboardingStatus:
    has_admin: bool
    model_profile_ids: list[str]
    main_agent_configured: bool


def extract_prompt_from_doc(path: Path) -> str:
    """Return the fenced markdown prompt from an agent documentation page."""

    content = path.read_text(encoding="utf-8")
    marker = "## Prompt"
    start = content.index(marker)
    match = re.search(r"(`{3,})markdown\s*\n(.*?)\n\1", content[start:], re.DOTALL)
    if match is None:
        raise RuntimeError(f"Could not find a markdown prompt fence in {path}.")
    return match.group(2).strip()


async def _find_agent_by_name(context: ApiContext, name: str) -> AgentDefinition | None:
    for agent in await context.agent_repo.list(include_deleted=True):
        if agent.name.strip().lower() == name.strip().lower():
            return agent
    return None


def _metadata_with(existing: FrameworkHints | None, **values: object) -> FrameworkHints:
    hints = existing or FrameworkHints()
    metadata = dict(hints.metadata)
    metadata.update(values)
    return hints.model_copy(update={"metadata": metadata})


async def setup_main_agent(*, interactive: bool, context: ApiContext | None = None) -> str | None:
    context = context or get_default_api_context()
    service = MainAgentSetupService(context)
    try:
        profile = await service.ensure_startup_ready(
            interactive=interactive,
            settings=get_settings(),
            default_agent_instructions=extract_prompt_from_doc(MAIN_AGENT_PROMPT_DOC),
        )
    except (MainAgentModelProfileRequiredError, MainAgentSetupRequiredError, MainAgentSetupInvalidError) as exc:
        print(service.startup_guidance(exc))
        return None
    print(f"Active main-agent profile: {profile.id}")
    return profile.id


async def sync_main_agent_prompt(*, context: ApiContext | None = None) -> int:
    context = context or get_default_api_context()
    service = MainAgentSetupService(context)
    try:
        agent = await service.sync_active_main_agent_instructions(extract_prompt_from_doc(MAIN_AGENT_PROMPT_DOC))
    except (MainAgentModelProfileRequiredError, MainAgentSetupRequiredError, MainAgentSetupInvalidError) as exc:
        print(service.startup_guidance(exc))
        return 1
    print(f"Synced main-agent instructions from docs/main-agent.md for agent: {agent.id}")
    return 0


async def check_main_agent(*, context: ApiContext | None = None) -> int:
    context = context or get_default_api_context()
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


async def _local_onboarding_status(context: ApiContext) -> LocalOnboardingStatus:
    users = await context.user_repo.list()
    has_admin = any("admin" in user.roles for user in users)
    profiles = await context.model_profile_repo.list()
    main_agent_configured = await MainAgentSetupService(context).is_main_agent_setup_complete()
    return LocalOnboardingStatus(
        has_admin=has_admin,
        model_profile_ids=[profile.id for profile in profiles],
        main_agent_configured=main_agent_configured,
    )


def _prompt_text_required(label: str, *, default: str | None = None) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        raw = input(f"{label}{suffix}: ").strip()
        if raw:
            return raw
        if default is not None:
            return default
        print(f"{label} is required.")


def _prompt_text_optional(label: str, *, default: str | None = None) -> str | None:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{label}{suffix}: ").strip()
    if raw:
        return raw
    return default.strip() if isinstance(default, str) and default.strip() else None


def _prompt_secret_with_confirmation(label: str) -> str:
    while True:
        first = getpass(f"{label}: ").strip()
        second = getpass(f"Confirm {label.lower()}: ").strip()
        if not first:
            print(f"{label} is required.")
            continue
        if first != second:
            print("Values did not match. Try again.")
            continue
        return first


def _prompt_choice(label: str, options: list[tuple[str, str]], *, default_value: str | None = None) -> str:
    print(label)
    for index, (_, text) in enumerate(options, start=1):
        print(f"  {index}. {text}")
    default_index = 1
    if default_value is not None:
        for index, (value, _) in enumerate(options, start=1):
            if value == default_value:
                default_index = index
                break
    while True:
        raw = input(f"Select option [1-{len(options)}] [{default_index}]: ").strip()
        if not raw:
            return options[default_index - 1][0]
        if raw.isdigit():
            selected_index = int(raw)
            if 1 <= selected_index <= len(options):
                return options[selected_index - 1][0]
        normalized = raw.lower()
        for value, _ in options:
            if normalized == value.lower():
                return value
        print("Select one of the listed options.")


def _prompt_bool(label: str, *, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{label} [{suffix}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("Answer yes or no.")


async def _prompt_for_local_admin(context: ApiContext) -> None:
    service = LocalAuthService(context)
    print("\nStep 1: Create the first local admin.")
    while True:
        display_name = _prompt_text_optional("Admin display name", default="Local Admin")
        email = _prompt_text_required("Admin email")
        password = _prompt_secret_with_confirmation("Admin password")
        try:
            result = await service.bootstrap_local_admin(
                email=email,
                password=password,
                display_name=display_name,
            )
        except LocalAuthBootstrapUnavailableError:
            print("A local admin already exists for this backend.")
            return
        except ValueError as exc:
            print(str(exc))
            continue
        print(f"Created local admin: {result.user.email}")
        return


async def _prompt_for_model_profile(context: ApiContext) -> str:
    profiles = await context.model_profile_repo.list()
    if profiles:
        print("\nExisting model profiles:")
        for index, profile in enumerate(profiles, start=1):
            print(f"  {index}. {profile.name} ({profile.model}) [{profile.provider}]")
        options = [(profile.id, f"Use {profile.name} ({profile.model})") for profile in profiles]
        options.append(("__new__", "Create a new setup model profile"))
        choice = _prompt_choice("Choose a model profile for the main agent.", options, default_value=profiles[0].id)
        if choice != "__new__":
            return choice

    service = SetupOnboardingService(context)
    provider = _prompt_choice(
        "\nStep 2: Choose a model provider.",
        [
            ("openai", "OpenAI"),
            ("ollama", "Ollama"),
        ],
        default_value="openai",
    )
    model_default = "gpt-4.1-mini" if provider == "openai" else "llama3.1:8b"
    model = _prompt_text_required("Model name", default=model_default)
    api_key: str | None = None
    base_url: str | None = None
    if provider == "openai":
        api_key = getpass("OpenAI API key: ").strip()
        if not api_key:
            raise RuntimeError("OpenAI setup requires an API key.")
        base_url = _prompt_text_optional("OpenAI base URL", default="https://api.openai.com/v1")
    else:
        base_url = _prompt_text_optional("Ollama base URL", default="http://localhost:11434")

    profile = await service.ensure_model_profile(
        provider_key=provider,
        model_name=model,
        api_key=api_key,
        base_url=base_url,
    )
    print(f"Configured model profile: {profile.name} ({profile.id})")
    return profile.id


async def setup_local_onboarding(*, context: ApiContext | None = None) -> int:
    context = context or get_default_api_context()
    status = await _local_onboarding_status(context)

    print("Agency terminal onboarding")
    print("This flow mirrors the browser /setup path for local installs.\n")

    if not status.has_admin:
        await _prompt_for_local_admin(context)
        status = await _local_onboarding_status(context)
    else:
        print("Step 1: Local admin already exists.")

    selected_profile_id = status.model_profile_ids[0] if status.model_profile_ids else None
    if not status.model_profile_ids:
        selected_profile_id = await _prompt_for_model_profile(context)
        status = await _local_onboarding_status(context)
    else:
        selected_profile_id = await _prompt_for_model_profile(context)

    if not status.main_agent_configured:
        print("\nStep 3: Finish main-agent setup.")
        agent_name = _prompt_text_required("Main agent name", default="Main Agent")
        profile = await SetupOnboardingService(context).ensure_main_agent(
            model_profile_id=selected_profile_id,
            agent_name=agent_name,
        )
        print(f"Configured main-agent profile: {profile.id}")
    else:
        print("Step 3: Main agent already exists.")
        if selected_profile_id is not None:
            profile = await SetupOnboardingService(context).ensure_main_agent(
                model_profile_id=selected_profile_id,
                agent_name="Main Agent",
            )
            print(f"Verified main-agent profile: {profile.id}")

    if _prompt_bool("\nQuick setup recommended supporting agents (Coder, Embedding, Evaluation)?", default=True):
        result = await SetupOnboardingService(context).ensure_recommended_agents()
        print("Recommended supporting agents are configured.")
        if result.coder_agent_id:
            print(f"  coder_agent_id: {result.coder_agent_id}")
        if result.embedding_agent_id:
            print(f"  embedding_agent_id: {result.embedding_agent_id}")
        if result.evaluation_agent_id:
            print(f"  evaluation_agent_id: {result.evaluation_agent_id}")

    final_status = await _local_onboarding_status(context)
    if final_status.has_admin and final_status.model_profile_ids and final_status.main_agent_configured:
        print("\nLocal onboarding is complete.")
        if final_status.model_profile_ids:
            print(f"  default_model_profile_id: {selected_profile_id}")
        print("You can now continue with:")
        print("  ./agency start")
        print("  python -m app.cli chat-main-agent")
        return 0

    print("\nLocal onboarding is still incomplete.")
    return 1


async def _find_codex_model_profile_id(context: ApiContext) -> str | None:
    profiles = await context.model_profile_repo.list()
    for profile in profiles:
        if str(profile.provider or "").strip().lower().replace("_", "-") == "openai-codex":
            return profile.id
    for profile in profiles:
        haystack = " ".join(
            str(part or "")
            for part in [profile.id, profile.name, profile.provider, profile.model]
        ).lower()
        if "codex" in haystack:
            return profile.id
    return None


async def setup_coder_agent(
        *,
        name: str = CODER_DEFAULT_NAME,
        role: str = CODER_DEFAULT_ROLE,
        model_profile_id: str | None = None,
        context: ApiContext | None = None,
) -> AgentDefinition:
    context = context or get_default_api_context()
    await context.ensure_builtin_tool_seed_data()

    command_tool = await context.tool_repo.get(SYSTEM_COMMAND_RUN_TOOL_ID)
    if command_tool is None:
        raise RuntimeError(f"Required tool was not seeded: {SYSTEM_COMMAND_RUN_TOOL_ID}")

    existing = await _find_agent_by_name(context, name)
    resolved_model_profile_id = model_profile_id or (existing.model_profile_id if existing else None)
    if resolved_model_profile_id is None:
        resolved_model_profile_id = await _find_codex_model_profile_id(context)

    prompt = extract_prompt_from_doc(CODER_PROMPT_DOC)
    tool_ids = list(existing.tool_ids if existing else [])
    if SYSTEM_COMMAND_RUN_TOOL_ID not in tool_ids:
        tool_ids.append(SYSTEM_COMMAND_RUN_TOOL_ID)
    graph_context = existing.graph_context if existing else GraphContextSettings()
    if await _ensure_graph_context_tool_if_enabled(context):
        tool_ids = _append_tool(tool_ids, SYSTEM_GRAPH_CONTEXT_TOOL_ID)
        graph_context = GraphContextSettings(
            enabled=True,
            auto_retrieval_enabled=True,
            coding_agent_resume_enabled=True,
            default_intent="resume",
            default_budget="balanced",
            include_memories=True,
            include_events=True,
            include_raw_graph=False,
            max_records=50,
            config={"allowed_intents": ["resume", "debug", "learn"]},
        )

    agent = AgentDefinition(
        id=existing.id if existing else "coder",
        name=name,
        display_name=existing.display_name if existing else name,
        description="CLI-first Codex coding worker for Agency workflows.",
        instructions=prompt,
        system_prompt=prompt,
        role=role,
        model_profile_id=resolved_model_profile_id,
        tool_ids=tool_ids,
        handoff_agent_ids=existing.handoff_agent_ids if existing else [],
        guardrails=existing.guardrails if existing else [],
        memory=existing.memory if existing else MemorySettings(),
        graph_context=graph_context,
        framework_hints=_metadata_with(
            existing.framework_hints if existing else None,
            agent_kind="coder",
            runtime_role="software_engineer",
            managed_by=MANAGED_BY,
        ),
        metadata={**(existing.metadata if existing else {}), "enabled": True, "agent_kind": "coder"},
    )
    return await context.agent_repo.save(agent)


def _default_base_url() -> str:
    return os.getenv("OLLAMA_BASE_URL") or "http://localhost:11434"


def _resolved_provider_base_url(
        existing: ModelProviderDefinition | None,
        explicit_base_url: str | None,
) -> str:
    if explicit_base_url:
        return explicit_base_url
    if existing is not None and existing.endpoint is not None and existing.endpoint.base_url:
        return existing.endpoint.base_url
    return _default_base_url()


async def _ensure_ollama_provider(
        context: ApiContext,
        *,
        provider_id: str,
        provider_name: str,
        base_url: str | None,
) -> ModelProviderDefinition:
    existing = await context.model_provider_repo.get(provider_id, include_deleted=True)
    resolved_base_url = _resolved_provider_base_url(existing, base_url)
    config = dict(existing.config if existing else {})
    config.update(
        {
            "provider_family": "ollama",
            "managed_by": MANAGED_BY,
        }
    )
    provider = ModelProviderDefinition(
        id=existing.id if existing else provider_id,
        name=provider_name or (existing.name if existing else DEFAULT_PROVIDER_NAME),
        provider_type=ModelProviderType.OLLAMA,
        description=existing.description if existing else "Local Ollama model provider.",
        endpoint={"base_url": resolved_base_url},
        config=config,
    )
    return await context.model_provider_repo.save(provider)


def _profile_parameters(existing: ModelProfileDefinition | None, *, model: str) -> dict[str, object]:
    parameters = dict(existing.parameters if existing else {})
    parameters.update(
        {
            "purpose": "memory_embedding",
            "embedding": True,
            "embedding_endpoint": "/api/embed",
            "ollama_model": model,
            "managed_by": MANAGED_BY,
        }
    )
    return parameters


def _profile_hints(existing: ModelProfileDefinition | None) -> FrameworkHints:
    return _metadata_with(
        existing.framework_hints if existing else None,
        purpose="memory_embedding",
        embedding_profile=True,
        managed_by=MANAGED_BY,
    )


async def _ensure_embedding_model_profile(
        context: ApiContext,
        *,
        provider: ModelProviderDefinition,
        profile_id: str,
        profile_name: str,
        model: str,
        base_url: str | None,
) -> ModelProfileDefinition:
    existing = await context.model_profile_repo.get(profile_id, include_deleted=True)
    resolved_base_url = (
            base_url
            or (existing.base_url if existing else None)
            or (provider.endpoint.base_url if provider.endpoint else None)
            or _default_base_url()
    )
    profile = ModelProfileDefinition(
        id=existing.id if existing else profile_id,
        name=profile_name or (existing.name if existing else DEFAULT_MODEL_PROFILE_NAME),
        provider=provider.id,
        model=model,
        description="Ollama embedding profile for Agency durable-memory vectorization.",
        base_url=resolved_base_url,
        temperature=0.0,
        max_tokens=None,
        supports_tools=False,
        supports_structured_output=False,
        supports_vision=False,
        supports_streaming=False,
        parameters=_profile_parameters(existing, model=model),
        framework_hints=_profile_hints(existing),
    )
    return await context.model_profile_repo.save(profile)


def _embedding_agent_hints(existing: AgentDefinition | None, *, model_profile_id: str) -> FrameworkHints:
    return _metadata_with(
        existing.framework_hints if existing else None,
        agent_kind="embedding",
        runtime_role="memory_embedding",
        embedding_model_profile_id=model_profile_id,
        managed_by=MANAGED_BY,
    )


async def setup_embedding_agent(
        *,
        name: str = DEFAULT_NAME,
        role: str = DEFAULT_ROLE,
        model: str = DEFAULT_MODEL,
        model_profile_id: str = DEFAULT_MODEL_PROFILE_ID,
        model_profile_name: str = DEFAULT_MODEL_PROFILE_NAME,
        provider_id: str = DEFAULT_PROVIDER_ID,
        provider_name: str = DEFAULT_PROVIDER_NAME,
        base_url: str | None = None,
        context: ApiContext | None = None,
) -> EmbeddingAgentSetupResult:
    context = context or get_default_api_context()
    provider = await _ensure_ollama_provider(
        context,
        provider_id=provider_id,
        provider_name=provider_name,
        base_url=base_url,
    )
    model_profile = await _ensure_embedding_model_profile(
        context,
        provider=provider,
        profile_id=model_profile_id,
        profile_name=model_profile_name,
        model=model,
        base_url=base_url,
    )
    existing = await _find_agent_by_name(context, name)
    prompt = extract_prompt_from_doc(EMBEDDING_PROMPT_DOC)
    agent = AgentDefinition(
        id=existing.id if existing else "embedding",
        name=name,
        display_name=existing.display_name if existing else name,
        description="Embedding worker profile for Agency durable-memory vectorization.",
        instructions=prompt,
        system_prompt=prompt,
        role=role,
        model_profile_id=model_profile.id,
        tool_ids=list(existing.tool_ids if existing else []),
        handoff_agent_ids=existing.handoff_agent_ids if existing else [],
        guardrails=existing.guardrails if existing else [],
        memory=existing.memory if existing else MemorySettings(enabled=False, strategy="embedding_profile"),
        graph_context=GraphContextSettings(
            enabled=False,
            auto_retrieval_enabled=False,
            include_raw_graph=False,
            config={"disabled_by_default": "lineage_debugging_only"},
        ),
        framework_hints=_embedding_agent_hints(existing, model_profile_id=model_profile.id),
        metadata={**(existing.metadata if existing else {}), "enabled": True, "agent_kind": "embedding"},
    )
    saved_agent = await context.agent_repo.save(agent)
    return EmbeddingAgentSetupResult(provider=provider, model_profile=model_profile, agent=saved_agent)


async def sync_embedding_agent_tool_access(*, context: ApiContext | None = None) -> AgentDefinition | None:
    context = context or get_default_api_context()
    existing = await _find_agent_by_name(context, DEFAULT_NAME)
    if existing is None:
        return None
    synced = existing.model_copy(update={"tool_ids": []})
    return await context.agent_repo.save(synced)


def _agent_kind(agent: AgentDefinition) -> str:
    hints_kind = agent.framework_hints.metadata.get("agent_kind")
    metadata_kind = agent.metadata.get("agent_kind")
    return str(hints_kind or metadata_kind or "").strip().lower()


async def _reserved_model_profile_ids(context: ApiContext) -> set[str]:
    reserved: set[str] = set()
    for main_profile in await context.main_agent_profile_repo.list(include_deleted=True):
        if not main_profile.enabled:
            continue
        if main_profile.default_model_profile_id:
            reserved.add(main_profile.default_model_profile_id)
        agent = await context.agent_repo.get(main_profile.agent_id, include_deleted=True)
        if agent is not None and agent.model_profile_id:
            reserved.add(agent.model_profile_id)

    for agent in await context.agent_repo.list(include_deleted=True):
        kind = _agent_kind(agent)
        if agent.name.strip().lower() in {"coder", "embedding"} or kind in {"coder", "coding", "embedding"}:
            if agent.model_profile_id:
                reserved.add(agent.model_profile_id)
    return reserved


def _is_evaluation_profile(profile: ModelProfileDefinition) -> bool:
    metadata = profile.framework_hints.metadata
    purpose = str(profile.parameters.get("purpose") or metadata.get("purpose") or "").strip().lower()
    if purpose in {"evaluation", "eval", "judge", "eval_judge"}:
        return True
    if profile.parameters.get("evaluator_profile") is True or metadata.get("evaluator_profile") is True:
        return True
    haystack = " ".join(str(part or "") for part in [profile.id, profile.name, profile.description]).lower()
    return "evaluation" in haystack or "evaluator" in haystack or "judge" in haystack


def _is_embedding_profile(profile: ModelProfileDefinition) -> bool:
    metadata = profile.framework_hints.metadata
    purpose = str(profile.parameters.get("purpose") or metadata.get("purpose") or "").strip().lower()
    return purpose in {"memory_embedding", "embedding"} or profile.parameters.get("embedding") is True


async def _select_evaluation_model_profile_id(
        context: ApiContext,
        *,
        reserved_model_profile_ids: set[str],
        existing: AgentDefinition | None,
) -> str | None:
    if existing is not None and existing.model_profile_id and existing.model_profile_id not in reserved_model_profile_ids:
        return existing.model_profile_id
    profiles = await context.model_profile_repo.list()
    available = [profile for profile in profiles if profile.id not in reserved_model_profile_ids]
    preferred = [profile for profile in available if _is_evaluation_profile(profile)]
    if preferred:
        return preferred[0].id
    non_embedding = [profile for profile in available if not _is_embedding_profile(profile)]
    if non_embedding:
        return non_embedding[0].id
    return available[0].id if available else None


async def _validate_explicit_model_profile_id(
        context: ApiContext,
        model_profile_id: str,
        *,
        reserved_model_profile_ids: set[str],
) -> str:
    profile = await context.model_profile_repo.get(model_profile_id)
    if profile is None:
        raise RuntimeError(f"Evaluation model profile '{model_profile_id}' was not found.")
    if model_profile_id in reserved_model_profile_ids:
        raise RuntimeError(
            f"Evaluation model profile '{model_profile_id}' is already used by the main, Coder, or Embedding agent."
        )
    return profile.id


def _evaluation_agent_hints(existing: AgentDefinition | None, *, model_profile_id: str | None) -> FrameworkHints:
    return _metadata_with(
        existing.framework_hints if existing else None,
        agent_kind="evaluation",
        runtime_role="eval_judge",
        evaluation_model_profile_id=model_profile_id,
        managed_by=MANAGED_BY,
    )


async def setup_evaluation_agent(
        *,
        name: str = EVALUATION_DEFAULT_NAME,
        role: str = EVALUATION_DEFAULT_ROLE,
        model_profile_id: str | None = None,
        context: ApiContext | None = None,
) -> EvaluationAgentSetupResult:
    context = context or get_default_api_context()
    resolver = AgentToolResolver(context)
    await resolver.ensure_workflow_system_tools(can_trigger_workflows=True)
    await resolver.ensure_execution_system_tools(can_inspect_executions=True)

    existing = await _find_agent_by_name(context, name)
    reserved_ids = await _reserved_model_profile_ids(context)
    resolved_model_profile_id = (
        await _validate_explicit_model_profile_id(context, model_profile_id, reserved_model_profile_ids=reserved_ids)
        if model_profile_id
        else await _select_evaluation_model_profile_id(
            context,
            reserved_model_profile_ids=reserved_ids,
            existing=existing,
        )
    )
    model_profile = (
        await context.model_profile_repo.get(resolved_model_profile_id)
        if resolved_model_profile_id is not None
        else None
    )
    tool_ids = list(READ_ONLY_TOOL_IDS)
    graph_context = GraphContextSettings(enabled=False, auto_retrieval_enabled=False)
    if await _ensure_graph_context_tool_if_enabled(context):
        tool_ids = _append_tool(tool_ids, SYSTEM_GRAPH_CONTEXT_TOOL_ID)
        graph_context = GraphContextSettings(
            enabled=True,
            auto_retrieval_enabled=False,
            default_intent="audit",
            default_budget="brief",
            include_memories=False,
            include_events=True,
            include_raw_graph=False,
            config={"read_only": True, "sensitive_memory_content": False},
        )

    prompt = extract_prompt_from_doc(EVALUATION_PROMPT_DOC)
    agent = AgentDefinition(
        id=existing.id if existing else "evaluation",
        name=name,
        display_name=existing.display_name if existing else name,
        description="Read-only semantic judge for Agency evaluation runs.",
        instructions=prompt,
        system_prompt=prompt,
        role=role,
        model_profile_id=resolved_model_profile_id,
        tool_ids=tool_ids,
        handoff_agent_ids=existing.handoff_agent_ids if existing else [],
        guardrails=existing.guardrails if existing else [],
        memory=MemorySettings(enabled=False, strategy="evaluation_judge"),
        graph_context=graph_context,
        framework_hints=_evaluation_agent_hints(existing, model_profile_id=resolved_model_profile_id),
        metadata={
            **(existing.metadata if existing else {}),
            "enabled": True,
            "agent_kind": "evaluation",
            "runtime_role": "eval_judge",
        },
    )
    saved_agent = await context.agent_repo.save(agent)
    return EvaluationAgentSetupResult(
        agent=saved_agent,
        model_profile=model_profile,
        reserved_model_profile_ids=reserved_ids,
    )


async def setup_all_agents(*, interactive: bool, context: ApiContext | None = None) -> AgentSetupResult:
    """Set up every built-in agent profile used by a fresh Agency deployment."""

    context = context or get_default_api_context()
    main_profile_id = await setup_main_agent(interactive=interactive, context=context)
    coder = await setup_coder_agent(context=context)
    embedding = await setup_embedding_agent(context=context)
    evaluation = await setup_evaluation_agent(context=context)
    return AgentSetupResult(
        main_agent_profile_id=main_profile_id,
        coder_agent=coder,
        embedding=embedding,
        evaluation=evaluation,
    )


async def sync_recommended_agents(*, context: ApiContext | None = None) -> RecommendedAgentsSyncResult:
    """Sync the recommended supporting agents that local onboarding can provision automatically."""

    context = context or get_default_api_context()
    coder = await setup_coder_agent(context=context)
    embedding = await setup_embedding_agent(context=context)
    evaluation = await setup_evaluation_agent(context=context)
    return RecommendedAgentsSyncResult(
        coder_agent=coder,
        embedding=embedding,
        evaluation=evaluation,
    )


def _print_embedding_result(result: EmbeddingAgentSetupResult) -> None:
    print("Embedding agent setup complete.")
    print(f"  agent_id: {result.agent.id}")
    print(f"  agent_name: {result.agent.name}")
    print(f"  provider_id: {result.provider.id}")
    print(f"  model_profile_id: {result.model_profile.id}")
    print(f"  model: {result.model_profile.model}")
    print(f"  base_url: {result.model_profile.base_url or '(provider/env default)'}")
    print("To activate durable-memory embeddings, configure:")
    print("  MEMORY_VECTOR_RETRIEVAL_ENABLED=true")
    print(f"  MEMORY_EMBEDDING_MODEL_PROFILE_ID={result.model_profile.id}")
    print("Then backfill existing memories with POST /memories/embeddings/backfill.")


def _print_evaluation_result(result: EvaluationAgentSetupResult) -> None:
    print("Evaluation agent setup complete.")
    print(f"  agent_id: {result.agent.id}")
    print(f"  agent_name: {result.agent.name}")
    print(f"  model_profile_id: {result.agent.model_profile_id or '(not set)'}")
    print(f"  tool_ids: {', '.join(result.agent.tool_ids)}")
    if result.agent.model_profile_id is None:
        print(
            "  warning: no model profile distinct from main, Coder, and Embedding was found; "
            "create an evaluator profile or pass --model-profile-id."
        )


def _add_non_interactive_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Use MAIN_AGENT_BOOTSTRAP_* environment variables instead of interactive prompts.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Set up Agency agents and their canonical prompts.")
    _add_non_interactive_argument(parser)
    subparsers = parser.add_subparsers(dest="command")

    all_parser = subparsers.add_parser("all", help="Legacy aggregate setup for main plus recommended agents.")
    _add_non_interactive_argument(all_parser)

    subparsers.add_parser(
        "recommended-agents",
        help="Sync the recommended Coder, Embedding, and Evaluation agents after onboarding.",
    )

    main_parser = subparsers.add_parser("main-agent", help="Set up the default main-agent profile.")
    _add_non_interactive_argument(main_parser)
    main_parser.add_argument(
        "--print-default-instructions",
        action="store_true",
        help="Print the built-in default main-agent instructions and exit.",
    )

    coder_parser = subparsers.add_parser("coder-agent", help="Create or update the Coder agent.")
    coder_parser.add_argument("--name", default=CODER_DEFAULT_NAME, help="Agent display name.")
    coder_parser.add_argument("--role", default=CODER_DEFAULT_ROLE, help="Agent role.")
    coder_parser.add_argument("--model-profile-id", help="Optional explicit model profile id.")

    embedding_parser = subparsers.add_parser("embedding-agent", help="Create or update the Embedding agent.")
    embedding_parser.add_argument("--name", default=DEFAULT_NAME, help="Agent display name.")
    embedding_parser.add_argument("--role", default=DEFAULT_ROLE, help="Agent role.")
    embedding_parser.add_argument("--model", default=DEFAULT_MODEL, help="Ollama model name for embedding calls.")
    embedding_parser.add_argument("--model-profile-id", default=DEFAULT_MODEL_PROFILE_ID, help="Embedding profile id.")
    embedding_parser.add_argument(
        "--model-profile-name",
        default=DEFAULT_MODEL_PROFILE_NAME,
        help="Embedding model profile name.",
    )
    embedding_parser.add_argument("--provider-id", default=DEFAULT_PROVIDER_ID, help="Model provider id.")
    embedding_parser.add_argument("--provider-name", default=DEFAULT_PROVIDER_NAME, help="Model provider name.")
    embedding_parser.add_argument(
        "--base-url",
        help="Ollama base URL. Defaults to OLLAMA_BASE_URL or http://localhost:11434.",
    )
    embedding_parser.add_argument(
        "--print-default-instructions",
        action="store_true",
        help="Print the built-in default embedding-agent instructions and exit.",
    )

    evaluation_parser = subparsers.add_parser("evaluation-agent", help="Create or update the Evaluation agent.")
    evaluation_parser.add_argument("--name", default=EVALUATION_DEFAULT_NAME, help="Agent display name.")
    evaluation_parser.add_argument("--role", default=EVALUATION_DEFAULT_ROLE, help="Agent role.")
    evaluation_parser.add_argument("--model-profile-id", help="Optional explicit evaluator model profile id.")
    evaluation_parser.add_argument(
        "--print-default-instructions",
        action="store_true",
        help="Print the built-in default evaluation-agent instructions and exit.",
    )

    subparsers.add_parser(
        "sync-main-agent-prompt",
        help="Update the active main agent with the canonical prompt from docs/main-agent.md.",
    )
    subparsers.add_parser("check-main-agent", help="Check whether main-agent setup is complete.")
    subparsers.add_parser(
        "local-onboarding",
        help="Run terminal onboarding for local admin, model profile, and main-agent setup.",
    )
    return parser


async def run_from_args(args: argparse.Namespace) -> int:
    command = args.command or "all"
    non_interactive = bool(getattr(args, "non_interactive", False))
    if command == "all":
        result = await setup_all_agents(interactive=not non_interactive)
        print("Agent setup complete.")
        print(f"  main_agent_profile_id: {result.main_agent_profile_id or '(not configured)'}")
        print(f"  coder_agent_id: {result.coder_agent.id if result.coder_agent else '(not configured)'}")
        print(f"  embedding_agent_id: {result.embedding.agent.id}")
        print(f"  evaluation_agent_id: {result.evaluation.agent.id}")
        if result.main_agent_profile_id is None:
            return 1
        return 0
    if command == "recommended-agents":
        result = await sync_recommended_agents()
        print("Recommended agent sync complete.")
        print(f"  coder_agent_id: {result.coder_agent.id}")
        print(f"  embedding_agent_id: {result.embedding.agent.id}")
        print(f"  evaluation_agent_id: {result.evaluation.agent.id}")
        return 0
    if command == "main-agent":
        if getattr(args, "print_default_instructions", False):
            print(extract_prompt_from_doc(MAIN_AGENT_PROMPT_DOC))
            return 0
        profile_id = await setup_main_agent(interactive=not non_interactive)
        return 0 if profile_id else 1
    if command == "coder-agent":
        agent = await setup_coder_agent(name=args.name, role=args.role, model_profile_id=args.model_profile_id)
        print("Coder agent setup complete.")
        print(f"  id: {agent.id}")
        print(f"  name: {agent.name}")
        print(f"  model_profile_id: {agent.model_profile_id or '(not set)'}")
        print(f"  tool_ids: {', '.join(agent.tool_ids)}")
        if agent.model_profile_id is None:
            print("  warning: no Codex model profile was found; set --model-profile-id or update it in the frontend.")
        return 0
    if command == "embedding-agent":
        if getattr(args, "print_default_instructions", False):
            print(extract_prompt_from_doc(EMBEDDING_PROMPT_DOC))
            return 0
        result = await setup_embedding_agent(
            name=args.name,
            role=args.role,
            model=args.model,
            model_profile_id=args.model_profile_id,
            model_profile_name=args.model_profile_name,
            provider_id=args.provider_id,
            provider_name=args.provider_name,
            base_url=args.base_url,
        )
        _print_embedding_result(result)
        return 0
    if command == "evaluation-agent":
        if getattr(args, "print_default_instructions", False):
            print(extract_prompt_from_doc(EVALUATION_PROMPT_DOC))
            return 0
        result = await setup_evaluation_agent(
            name=args.name,
            role=args.role,
            model_profile_id=args.model_profile_id,
        )
        _print_evaluation_result(result)
        return 0
    if command == "sync-main-agent-prompt":
        return await sync_main_agent_prompt()
    if command == "check-main-agent":
        return await check_main_agent()
    if command == "local-onboarding":
        return await setup_local_onboarding()
    raise RuntimeError(f"Unknown setup command: {command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(run_from_args(args))


if __name__ == "__main__":
    raise SystemExit(main())
