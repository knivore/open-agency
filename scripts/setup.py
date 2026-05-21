#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


def _bootstrap_repo(script_file: str, *, reexec: bool) -> Path:
    script_path = Path(script_file).resolve()
    repo_root = script_path.parents[1]
    if reexec:
        venv_dir = repo_root / ".venv"
        venv_python = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        if venv_python.exists() and Path(sys.prefix).resolve() != venv_dir.resolve():
            os.execv(str(venv_python), [str(venv_python), str(script_path), *sys.argv[1:]])
    repo_root_text = str(repo_root)
    if repo_root_text not in sys.path:
        sys.path.insert(0, repo_root_text)
    return repo_root


REPO_ROOT = _bootstrap_repo(__file__, reexec=__name__ == "__main__")

from app.api.context import ApiContext, get_default_api_context
from app.core.config import get_settings
from app.domain import (
    AgentDefinition,
    FrameworkHints,
    MainAgentProfile,
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
    SYSTEM_WORKFLOW_GET_TOOL_ID,
    SYSTEM_WORKFLOW_LIST_TOOL_ID,
)
from app.services.main_agent_setup import (
    MainAgentModelProfileRequiredError,
    MainAgentSetupInvalidError,
    MainAgentSetupRequiredError,
    MainAgentSetupService,
)


MAIN_AGENT_PROMPT_DOC = REPO_ROOT / "docs" / "main-agent.md"

CODER_DEFAULT_NAME = "Coder"
CODER_DEFAULT_ROLE = "Senior Software Engineer"
CODER_PROMPT_DOC = REPO_ROOT / "docs" / "coding-agent.md"

EMBEDDING_DEFAULT_NAME = "Embedding"
EMBEDDING_DEFAULT_ROLE = "Memory Embedding Worker"
DEFAULT_PROVIDER_ID = "ollama"
DEFAULT_PROVIDER_NAME = "Ollama"
DEFAULT_MODEL_PROFILE_ID = "embedding-nemotron-nano"
DEFAULT_MODEL_PROFILE_NAME = "Nemotron Nano Embeddings"
DEFAULT_MODEL = "huihui_ai/nemotron-v1-abliterated:8b-llama-3.1-nano"
EMBEDDING_PROMPT_DOC = REPO_ROOT / "docs" / "embedding-agent.md"

EVALUATION_DEFAULT_NAME = "Evaluation"
EVALUATION_DEFAULT_ROLE = "Evaluation Judge"
EVALUATION_PROMPT_DOC = REPO_ROOT / "docs" / "evaluation-agent.md"
READ_ONLY_TOOL_IDS = [
    SYSTEM_EXECUTION_GET_TOOL_ID,
    SYSTEM_EXECUTION_EVENTS_TOOL_ID,
    SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID,
    SYSTEM_WORKFLOW_GET_TOOL_ID,
    SYSTEM_WORKFLOW_LIST_TOOL_ID,
]

SETUP_ERRORS = (
    MainAgentModelProfileRequiredError,
    MainAgentSetupRequiredError,
    MainAgentSetupInvalidError,
)


@dataclass(slots=True)
class EmbeddingAgentSetupResult:
    provider: ModelProviderDefinition
    model_profile: ModelProfileDefinition
    agent: AgentDefinition


@dataclass(slots=True)
class EvaluationAgentSetupResult:
    agent: AgentDefinition
    model_profile: ModelProfileDefinition | None
    reserved_model_profile_ids: set[str]


def _extract_prompt_from_doc(path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    marker = "## Prompt"
    start = content.index(marker)
    for fence in ("````markdown", "```markdown"):
        try:
            fence_start = content.index(fence, start) + len(fence)
            fence_end = content.index(fence.split("markdown", 1)[0], fence_start)
            return content[fence_start:fence_end].strip()
        except ValueError:
            continue
    raise ValueError(f"No markdown prompt fence found in {path}")


async def setup_main_agent(*, interactive: bool, context: ApiContext | None = None) -> MainAgentProfile:
    context = context or get_default_api_context()
    service = MainAgentSetupService(context)
    return await service.ensure_startup_ready(
        interactive=interactive,
        settings=get_settings(),
        default_agent_instructions=_extract_prompt_from_doc(MAIN_AGENT_PROMPT_DOC),
    )


async def check_main_agent(context: ApiContext | None = None) -> MainAgentProfile:
    context = context or get_default_api_context()
    service = MainAgentSetupService(context)
    return await service.require_active_main_agent_profile()


async def sync_main_agent_prompt(context: ApiContext | None = None) -> AgentDefinition:
    context = context or get_default_api_context()
    service = MainAgentSetupService(context)
    return await service.sync_active_main_agent_instructions(_extract_prompt_from_doc(MAIN_AGENT_PROMPT_DOC))


async def _find_agent_by_name(context: ApiContext, name: str) -> AgentDefinition | None:
    for agent in await context.agent_repo.list(include_deleted=True):
        if agent.name.strip().lower() == name.strip().lower():
            return agent
    return None


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

    prompt = _extract_prompt_from_doc(CODER_PROMPT_DOC)
    tool_ids = list(existing.tool_ids if existing else [])
    if SYSTEM_COMMAND_RUN_TOOL_ID not in tool_ids:
        tool_ids.append(SYSTEM_COMMAND_RUN_TOOL_ID)

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
        framework_hints=existing.framework_hints if existing else FrameworkHints(metadata={"agent_kind": "coder"}),
        metadata={**(existing.metadata if existing else {}), "enabled": True, "agent_kind": "coder"},
    )
    return await context.agent_repo.save(agent)


def _default_embedding_base_url() -> str:
    return os.getenv("OLLAMA_BASE_URL") or "http://localhost:11434"


def _resolved_provider_base_url(
    existing: ModelProviderDefinition | None,
    explicit_base_url: str | None,
) -> str:
    if explicit_base_url:
        return explicit_base_url
    if existing is not None and existing.endpoint is not None and existing.endpoint.base_url:
        return existing.endpoint.base_url
    return _default_embedding_base_url()


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
    config.update({"provider_family": "ollama", "managed_by": "scripts/setup.py"})
    provider = ModelProviderDefinition(
        id=existing.id if existing else provider_id,
        name=provider_name or (existing.name if existing else DEFAULT_PROVIDER_NAME),
        provider_type=ModelProviderType.OLLAMA,
        description=existing.description if existing else "Local Ollama model provider.",
        endpoint={"base_url": resolved_base_url},
        config=config,
    )
    return await context.model_provider_repo.save(provider)


def _embedding_profile_parameters(existing: ModelProfileDefinition | None, *, model: str) -> dict[str, object]:
    parameters = dict(existing.parameters if existing else {})
    parameters.update(
        {
            "purpose": "memory_embedding",
            "embedding": True,
            "embedding_endpoint": "/api/embed",
            "ollama_model": model,
            "managed_by": "scripts/setup.py",
        }
    )
    return parameters


def _embedding_profile_hints(existing: ModelProfileDefinition | None) -> FrameworkHints:
    hints = existing.framework_hints if existing else FrameworkHints()
    metadata = dict(hints.metadata)
    metadata.update({"purpose": "memory_embedding", "embedding_profile": True, "managed_by": "scripts/setup.py"})
    return hints.model_copy(update={"metadata": metadata})


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
        or _default_embedding_base_url()
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
        parameters=_embedding_profile_parameters(existing, model=model),
        framework_hints=_embedding_profile_hints(existing),
    )
    return await context.model_profile_repo.save(profile)


def _embedding_agent_hints(existing: AgentDefinition | None, *, model_profile_id: str) -> FrameworkHints:
    hints = existing.framework_hints if existing else FrameworkHints()
    metadata = dict(hints.metadata)
    metadata.update(
        {
            "agent_kind": "embedding",
            "runtime_role": "memory_embedding",
            "embedding_model_profile_id": model_profile_id,
            "managed_by": "scripts/setup.py",
        }
    )
    return hints.model_copy(update={"metadata": metadata})


async def setup_embedding_agent(
    *,
    name: str = EMBEDDING_DEFAULT_NAME,
    role: str = EMBEDDING_DEFAULT_ROLE,
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
    prompt = _extract_prompt_from_doc(EMBEDDING_PROMPT_DOC)
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
        framework_hints=_embedding_agent_hints(existing, model_profile_id=model_profile.id),
        metadata={**(existing.metadata if existing else {}), "enabled": True, "agent_kind": "embedding"},
    )
    saved_agent = await context.agent_repo.save(agent)
    return EmbeddingAgentSetupResult(provider=provider, model_profile=model_profile, agent=saved_agent)


async def sync_embedding_agent_tool_access(context: ApiContext | None = None) -> AgentDefinition | None:
    context = context or get_default_api_context()
    existing = await _find_agent_by_name(context, EMBEDDING_DEFAULT_NAME)
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


async def _validate_explicit_evaluation_model_profile_id(
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
    hints = existing.framework_hints if existing else FrameworkHints()
    metadata = dict(hints.metadata)
    metadata.update(
        {
            "agent_kind": "evaluation",
            "runtime_role": "eval_judge",
            "evaluation_model_profile_id": model_profile_id,
            "managed_by": "scripts/setup.py",
        }
    )
    return hints.model_copy(update={"metadata": metadata})


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
        await _validate_explicit_evaluation_model_profile_id(
            context,
            model_profile_id,
            reserved_model_profile_ids=reserved_ids,
        )
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

    prompt = _extract_prompt_from_doc(EVALUATION_PROMPT_DOC)
    agent = AgentDefinition(
        id=existing.id if existing else "evaluation",
        name=name,
        display_name=existing.display_name if existing else name,
        description="Read-only semantic judge for Agency evaluation runs.",
        instructions=prompt,
        system_prompt=prompt,
        role=role,
        model_profile_id=resolved_model_profile_id,
        tool_ids=list(READ_ONLY_TOOL_IDS),
        handoff_agent_ids=existing.handoff_agent_ids if existing else [],
        guardrails=existing.guardrails if existing else [],
        memory=MemorySettings(enabled=False, strategy="evaluation_judge"),
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


def _print_coder_result(agent: AgentDefinition) -> None:
    print("Coder agent setup complete.")
    print(f"  id: {agent.id}")
    print(f"  name: {agent.name}")
    print(f"  model_profile_id: {agent.model_profile_id or '(not set)'}")
    print(f"  tool_ids: {', '.join(agent.tool_ids)}")
    if agent.model_profile_id is None:
        print("  warning: no Codex model profile was found; set --model-profile-id or update it in the frontend.")


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


async def _run_main_agent(args: argparse.Namespace) -> int:
    if getattr(args, "print_default_instructions", False):
        print(_extract_prompt_from_doc(MAIN_AGENT_PROMPT_DOC))
        return 0
    context = get_default_api_context()
    service = MainAgentSetupService(context)
    try:
        profile = await setup_main_agent(interactive=not args.non_interactive, context=context)
    except SETUP_ERRORS as exc:
        print(service.startup_guidance(exc))
        return 1
    print(f"Active main-agent profile: {profile.id}")
    return 0


async def _run_check_main_agent(args: argparse.Namespace) -> int:
    context = get_default_api_context()
    service = MainAgentSetupService(context)
    try:
        profile = await check_main_agent(context=context)
    except SETUP_ERRORS as exc:
        print(service.startup_guidance(exc))
        return 1
    print(
        "Main-agent setup is complete.\n"
        f"  profile_id: {profile.id}\n"
        f"  agent_id: {profile.agent_id}\n"
        f"  workflow_id: {profile.default_workflow_id}\n"
    )
    return 0


async def _run_sync_main_agent_prompt(args: argparse.Namespace) -> int:
    context = get_default_api_context()
    service = MainAgentSetupService(context)
    try:
        agent = await sync_main_agent_prompt(context=context)
    except SETUP_ERRORS as exc:
        print(service.startup_guidance(exc))
        return 1
    print(f"Synced main-agent instructions from docs/main-agent.md for agent: {agent.id}")
    return 0


async def _run_coder(args: argparse.Namespace) -> int:
    agent = await setup_coder_agent(
        name=args.name,
        role=args.role,
        model_profile_id=args.model_profile_id,
    )
    _print_coder_result(agent)
    return 0


async def _run_embedding(args: argparse.Namespace) -> int:
    if getattr(args, "print_default_instructions", False):
        print(_extract_prompt_from_doc(EMBEDDING_PROMPT_DOC))
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


async def _run_evaluation(args: argparse.Namespace) -> int:
    if getattr(args, "print_default_instructions", False):
        print(_extract_prompt_from_doc(EVALUATION_PROMPT_DOC))
        return 0
    result = await setup_evaluation_agent(
        name=args.name,
        role=args.role,
        model_profile_id=args.model_profile_id,
    )
    _print_evaluation_result(result)
    return 0


async def _run_all(args: argparse.Namespace) -> int:
    context = get_default_api_context()
    service = MainAgentSetupService(context)
    try:
        main_profile = await setup_main_agent(interactive=not args.non_interactive, context=context)
    except SETUP_ERRORS as exc:
        print(service.startup_guidance(exc))
        return 1
    print(f"Active main-agent profile: {main_profile.id}")

    coder = await setup_coder_agent(
        name=CODER_DEFAULT_NAME,
        role=CODER_DEFAULT_ROLE,
        context=context,
    )
    _print_coder_result(coder)

    embedding = await setup_embedding_agent(context=context)
    _print_embedding_result(embedding)

    evaluation = await setup_evaluation_agent(context=context)
    _print_evaluation_result(evaluation)

    print("All agent setup complete.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Set up Agency agents. With no command, this creates or updates the main, Coder, Embedding, "
            "and Evaluation agents."
        )
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="For full setup, use MAIN_AGENT_BOOTSTRAP_* environment variables instead of interactive prompts.",
    )
    subparsers = parser.add_subparsers(dest="command")

    def add_non_interactive(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--non-interactive", action="store_true", default=argparse.SUPPRESS)

    all_agents = subparsers.add_parser("all", help="Create or update all built-in agents.")
    add_non_interactive(all_agents)
    all_agents.set_defaults(handler=_run_all)

    main_agent = subparsers.add_parser("main-agent", help="Create or validate the default main agent.")
    add_non_interactive(main_agent)
    main_agent.add_argument("--print-default-instructions", action="store_true")
    main_agent.set_defaults(handler=_run_main_agent)

    check_main = subparsers.add_parser("check-main-agent", help="Check main-agent setup status.")
    check_main.set_defaults(handler=_run_check_main_agent)

    sync_prompt = subparsers.add_parser("sync-main-agent-prompt", help="Sync the canonical main-agent prompt.")
    sync_prompt.set_defaults(handler=_run_sync_main_agent_prompt)

    coder = subparsers.add_parser("coder-agent", help="Create or update the Coder agent.")
    coder.add_argument("--name", default=CODER_DEFAULT_NAME)
    coder.add_argument("--role", default=CODER_DEFAULT_ROLE)
    coder.add_argument("--model-profile-id")
    coder.set_defaults(handler=_run_coder)

    embedding = subparsers.add_parser("embedding-agent", help="Create or update the Embedding agent.")
    embedding.add_argument("--name", default=EMBEDDING_DEFAULT_NAME)
    embedding.add_argument("--role", default=EMBEDDING_DEFAULT_ROLE)
    embedding.add_argument("--model", default=DEFAULT_MODEL)
    embedding.add_argument("--model-profile-id", default=DEFAULT_MODEL_PROFILE_ID)
    embedding.add_argument("--model-profile-name", default=DEFAULT_MODEL_PROFILE_NAME)
    embedding.add_argument("--provider-id", default=DEFAULT_PROVIDER_ID)
    embedding.add_argument("--provider-name", default=DEFAULT_PROVIDER_NAME)
    embedding.add_argument("--base-url")
    embedding.add_argument("--print-default-instructions", action="store_true")
    embedding.set_defaults(handler=_run_embedding)

    evaluation = subparsers.add_parser("evaluation-agent", help="Create or update the Evaluation agent.")
    evaluation.add_argument("--name", default=EVALUATION_DEFAULT_NAME)
    evaluation.add_argument("--role", default=EVALUATION_DEFAULT_ROLE)
    evaluation.add_argument("--model-profile-id")
    evaluation.add_argument("--print-default-instructions", action="store_true")
    evaluation.set_defaults(handler=_run_evaluation)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", _run_all)
    return asyncio.run(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
