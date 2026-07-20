"""Main-agent bootstrap and startup readiness service.

The service owns the database shape behind local onboarding and headless
bootstrap: it verifies that a chat-capable model profile exists, creates or
validates the active ``MainAgentProfile``, and keeps the default workflow/agent
references intact. Launch scripts call ``scripts/setup.py`` for
terminal/operator setup, while application startup calls this service directly
to decide whether the API can safely accept main-agent conversation traffic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from app.core.config import Settings, get_settings
from app.domain import (
    AgentDefinition,
    Conversation,
    ConversationChannelType,
    GraphContextSettings,
    MainAgentProfile,
    ModelProfileDefinition,
    ModelProviderDefinition,
    ModelProviderType,
    NodeType,
    WorkflowDefinition,
    WorkflowNodeDefinition,
)
from app.services.agent_tools import AgentToolResolver, LEGACY_MAIN_AGENT_INTERNAL_TOOL_PREFIX

if TYPE_CHECKING:
    from app.api.context import ApiContext


def _graph_context_tools_allowed(policy: dict[str, Any]) -> bool:
    return bool(policy.get("can_read_graph_context", False) and get_settings().agency_graph_context_tools_enabled)


def _agent_graph_context_settings(policy: dict[str, Any]) -> GraphContextSettings:
    return GraphContextSettings(
        enabled=_graph_context_tools_allowed(policy),
        auto_retrieval_enabled=bool(policy.get("graph_context_auto_retrieval_enabled", False)),
        subagent_steering_enabled=bool(policy.get("graph_context_subagent_steering_enabled", False)),
        coding_agent_resume_enabled=bool(policy.get("graph_context_coding_agent_resume_enabled", False)),
        default_budget=policy.get("graph_context_default_budget"),
        default_intent=policy.get("graph_context_default_intent"),
    )


@dataclass(slots=True)
class MainAgentSetupConfig:
    """Fully resolved settings for creating the persisted main-agent bundle."""

    agent_name: str
    agent_instructions: str
    model_profile_id: str
    agent_description: str | None = None
    workflow_name: str = "Main Workflow"
    workflow_description: str | None = "Default workflow for main-agent orchestration."
    profile_name: str = "Main"
    profile_description: str | None = "Default main agent profile."
    policy: dict[str, Any] = field(
        default_factory=lambda: {
            "can_answer_directly": True,
            "can_trigger_workflows": True,
            "can_manage_goals": True,
            "can_create_workflows": False,
            "can_update_workflows": False,
            "can_manage_tools": True,
            "can_manage_agents": True,
            "can_manage_integrations": True,
            "can_inspect_executions": True,
            "can_run_commands": True,
            "can_read_graph_context": True,
            "graph_context_auto_retrieval_enabled": True,
            "graph_context_subagent_steering_enabled": True,
            "graph_context_coding_agent_resume_enabled": False,
            "graph_context_default_intent": "handoff",
            "graph_context_default_budget": "balanced",
            "require_approval_for_mutations": True,
            "enable_computer_use": True,
        }
    )
    agent_id: str | None = None
    workflow_id: str | None = None
    profile_id: str | None = None
    agent_metadata: dict[str, Any] = field(default_factory=dict)
    workflow_metadata: dict[str, Any] = field(default_factory=dict)
    profile_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MainAgentBootstrapConfig:
    """Environment-derived bootstrap settings for headless first-run setup."""

    existing_model_profile_id: str | None = None
    provider_family: str | None = None
    provider_name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    profile_name: str | None = None
    model_name: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    agent_name: str = "Main Agent"
    agent_description: str = "Default conversational orchestrator for this deployment."
    agent_instructions: str = ""
    workflow_name: str = "Main Workflow"
    workflow_description: str = "Default workflow for main-agent orchestration."
    can_trigger_workflows: bool = True
    can_create_workflows: bool = True
    can_update_workflows: bool = True
    require_approval_for_mutations: bool = True


class MainAgentSetupError(RuntimeError):
    pass


class MainAgentModelProfileRequiredError(MainAgentSetupError):
    pass


class MainAgentSetupRequiredError(MainAgentSetupError):
    pass


class MainAgentSetupInvalidError(MainAgentSetupError):
    pass


@dataclass(slots=True)
class MainAgentSetupService:
    """Coordinates model, agent, workflow, and profile records for the main agent."""

    context: ApiContext

    async def list_visible_computer_use_tools(self) -> list[Any]:
        tools = await self.context.tool_repo.list()
        return [
            tool
            for tool in tools
            if "computer_use" in getattr(tool, "tags", [])
               and tool.tool_type.value == "mcp_tool"
        ]

    async def list_usable_model_profiles(self) -> list[ModelProfileDefinition]:
        return await self.context.model_profile_repo.list()

    async def has_usable_model_profiles(self) -> bool:
        profiles = await self.list_usable_model_profiles()
        return len(profiles) > 0

    async def get_active_main_agent_profile(self) -> MainAgentProfile | None:
        profiles = await self.context.main_agent_profile_repo.list()
        enabled = [item for item in profiles if item.enabled]
        if not enabled:
            return None
        enabled.sort(key=lambda item: (item.created_at, item.id))
        profile = enabled[0]
        await self._assert_profile_references_are_valid(profile)
        return profile

    async def is_main_agent_setup_complete(self) -> bool:
        try:
            profile = await self.get_active_main_agent_profile()
        except MainAgentSetupInvalidError:
            return False
        return profile is not None

    async def require_usable_model_profiles(self) -> list[ModelProfileDefinition]:
        profiles = await self.list_usable_model_profiles()
        if not profiles:
            raise MainAgentModelProfileRequiredError(
                "Main-agent setup requires at least one configured model profile before the first main agent can be created."
            )
        return profiles

    async def require_active_main_agent_profile(self) -> MainAgentProfile:
        profile = await self.get_active_main_agent_profile()
        if profile is None:
            raise MainAgentSetupRequiredError(
                "Main-agent setup has not been completed. Configure the first main agent before using conversations."
            )
        return profile

    async def assert_startup_ready(self) -> None:
        await self.require_usable_model_profiles()
        await self.require_active_main_agent_profile()

    async def ensure_startup_ready(
            self,
            *,
            interactive: bool,
            settings: Settings | None = None,
            default_agent_instructions: str | None = None,
    ) -> MainAgentProfile:
        bootstrap = (
            self.bootstrap_config_from_settings(
                settings,
                default_agent_instructions=default_agent_instructions,
            )
            if settings is not None
            else None
        )
        profiles = await self.list_usable_model_profiles()
        if not profiles:
            if bootstrap is not None:
                profile = await self._create_model_profile_from_bootstrap(bootstrap)
                profiles = [profile]
            elif not interactive:
                raise MainAgentModelProfileRequiredError(
                    "Main-agent setup requires at least one configured model profile. "
                    "Complete interactive onboarding or configure MAIN_AGENT_BOOTSTRAP_* env vars."
                )
            else:
                profile = await self._prompt_and_create_model_profile()
                profiles = [profile]
        try:
            return await self.require_active_main_agent_profile()
        except MainAgentSetupRequiredError:
            if bootstrap is not None:
                created = await self.create_main_agent(
                    self._build_main_agent_config_from_bootstrap(bootstrap, profiles))
                print(
                    "\nMain-agent env bootstrap complete.\n"
                    f"  profile_id: {created.id}\n"
                    f"  agent_id: {created.agent_id}\n"
                    f"  workflow_id: {created.default_workflow_id}\n"
                )
                return created
            if not interactive:
                raise
        config = self._prompt_for_main_agent_config(
            profiles,
            default_agent_instructions=default_agent_instructions,
        )
        created = await self.create_main_agent(config)
        print(
            "\nMain-agent setup complete.\n"
            f"  profile_id: {created.id}\n"
            f"  agent_id: {created.agent_id}\n"
            f"  workflow_id: {created.default_workflow_id}\n"
        )
        return created

    @staticmethod
    def startup_guidance(exc: Exception) -> str:
        base = str(exc).strip() or exc.__class__.__name__
        return (
            f"{base}\n\n"
            "For normal local setup, complete onboarding in one of these ways:\n"
            "  browser path: open /setup when agency-fe is available\n"
            "  terminal path: python scripts/setup.py local-onboarding\n\n"
            "For the older operator-style main-agent flow, run:\n"
            "  python scripts/setup.py main-agent\n\n"
            "After setup, you can chat directly in the terminal with:\n"
            "  python -m app.cli chat-main-agent\n\n"
            "Or prepare Discord/Telegram/WhatsApp bot access and route chats through those channels instead.\n\n"
            "For headless startup, configure MAIN_AGENT_BOOTSTRAP_ENABLED=true and the required MAIN_AGENT_BOOTSTRAP_* variables.\n"
        )

    @staticmethod
    def chat_channel_setup_guidance(channel: str) -> str:
        normalized = channel.strip().lower()
        if normalized not in {"discord", "telegram", "whatsapp"}:
            raise MainAgentSetupInvalidError(
                "Chat channel guidance is only available for discord, telegram, or whatsapp.")

        extra_step: str
        webhook_url: str
        verification_step: str
        channel_step: str
        if normalized == "discord":
            verification_step = (
                "Configure the Discord interaction/webhook signature public key on the credential. "
                "Use the Discord application Public Key hex value, not a Discord webhook URL."
            )
            webhook_url = "/integrations/conversations/adapters/discord/webhook"
            channel_step = "If you want the same assistant to reply in a specific guild/channel, store the channel thread id on the conversation."
            extra_step = (
                "Use Discord components for approve/reject actions, and create a trusted channel identity mapping "
                "for your Discord user id before testing protected mutations."
            )
        elif normalized == "telegram":
            verification_step = "Configure the Telegram webhook secret token on the credential."
            webhook_url = "/integrations/conversations/adapters/telegram/webhook?credential_id=<installation_id>"
            channel_step = "Use inline keyboard callbacks for approve/reject actions."
            extra_step = (
                "Set up the bot token with BotFather, run Telegram getMe to capture the numeric bot user id, and confirm the webhook can reach the backend. "
                "Use the installation id as the credential_id query parameter so Agency can send replies back through the same installation."
            )
        else:
            verification_step = "Configure the WhatsApp app secret and phone number metadata on the credential."
            webhook_url = "/integrations/conversations/adapters/whatsapp/webhook"
            channel_step = "Use WhatsApp interactive reply buttons for approve/reject actions."
            extra_step = "Ensure the connected WhatsApp business number can receive webhook events."

        lines = [
            f"{normalized.title()} bot setup checklist:",
            "1. Create or select the bot/app in the provider console.",
            "2. Create a connector credential in Agency for that provider.",
            f"3. {verification_step}",
            f"4. Set the webhook URL to `{webhook_url}`.",
            "5. Map the external channel user id to an internal user if you want trusted approvals and mutations.",
            "6. Send a test message to confirm inbound and outbound chat both work.",
            f"7. {channel_step}",
            f"8. {extra_step}",
        ]
        if normalized == "discord":
            lines.extend(
                [
                    "",
                    "Discord-specific notes:",
                    "- Store the bot token as a secret reference, not as metadata.",
                    "- Store application_id, bot_user_id, default_guild_id, and webhook_public_key as metadata.",
                    "- webhook_public_key must be the Discord application Public Key hex string.",
                    "- The webhook/interactions endpoint URL and the public key are different values.",
                    "- After setup, run `python -m app.cli smoke-test-discord --owner-user-id YOUR_USER_ID --discord-user-id YOUR_DISCORD_USER_ID`.",
                ]
            )
        return "\n".join(lines)

    @staticmethod
    def bootstrap_config_from_settings(
            settings: Settings | None,
            *,
            default_agent_instructions: str | None = None,
    ) -> MainAgentBootstrapConfig | None:
        if settings is None or not settings.main_agent_bootstrap_enabled:
            return None

        def optional_text(value: str | None) -> str | None:
            return value.strip() or None if value is not None else None

        return MainAgentBootstrapConfig(
            existing_model_profile_id=optional_text(settings.main_agent_bootstrap_existing_model_profile_id),
            provider_family=optional_text(settings.main_agent_bootstrap_provider_family),
            provider_name=optional_text(settings.main_agent_bootstrap_provider_name),
            base_url=optional_text(settings.main_agent_bootstrap_base_url),
            api_key=optional_text(settings.main_agent_bootstrap_api_key),
            profile_name=optional_text(settings.main_agent_bootstrap_profile_name),
            model_name=optional_text(settings.main_agent_bootstrap_model_name),
            temperature=settings.main_agent_bootstrap_temperature,
            max_tokens=settings.main_agent_bootstrap_max_tokens,
            agent_name=settings.main_agent_bootstrap_agent_name or "Main Agent",
            agent_description=(
                    settings.main_agent_bootstrap_agent_description
                    or "Default conversational orchestrator for this deployment."
            ),
            agent_instructions=(
                    settings.main_agent_bootstrap_agent_instructions
                    or default_agent_instructions
                    or ""
            ),
            workflow_name=settings.main_agent_bootstrap_workflow_name or "Main Workflow",
            workflow_description=(
                    settings.main_agent_bootstrap_workflow_description or "Default workflow for main-agent orchestration."
            ),
            can_trigger_workflows=(
                True
                if settings.main_agent_bootstrap_can_trigger_workflows is None
                else settings.main_agent_bootstrap_can_trigger_workflows
            ),
            can_create_workflows=(
                True
                if settings.main_agent_bootstrap_can_create_workflows is None
                else settings.main_agent_bootstrap_can_create_workflows
            ),
            can_update_workflows=(
                True
                if settings.main_agent_bootstrap_can_update_workflows is None
                else settings.main_agent_bootstrap_can_update_workflows
            ),
            require_approval_for_mutations=(
                True
                if settings.main_agent_bootstrap_require_approval_for_mutations is None
                else settings.main_agent_bootstrap_require_approval_for_mutations
            ),
        )

    async def create_main_agent(self, config: MainAgentSetupConfig) -> MainAgentProfile:
        if not config.agent_name.strip():
            raise MainAgentSetupInvalidError("Main-agent setup requires a non-empty agent name.")
        if not config.agent_instructions.strip():
            raise MainAgentSetupInvalidError("Main-agent setup requires non-empty agent instructions.")

        model_profile = await self.context.model_profile_repo.get(config.model_profile_id)
        if model_profile is None:
            raise MainAgentModelProfileRequiredError(
                f"Model profile '{config.model_profile_id}' was not found for main-agent setup."
            )

        tool_resolver = AgentToolResolver(self.context)
        system_tools = await tool_resolver.ensure_workflow_system_tools(
            can_trigger_workflows=config.policy.get("can_trigger_workflows", True)
        )
        system_tools.extend(
            await tool_resolver.ensure_goal_system_tools(
                can_manage_goals=config.policy.get("can_manage_goals", True)
            )
        )
        system_tools.extend(await tool_resolver.ensure_default_main_agent_speech_tools())
        system_tools.extend(
            await tool_resolver.ensure_tool_management_system_tools(
                can_manage_tools=config.policy.get("can_manage_tools", True)
            )
        )
        system_tools.extend(
            await tool_resolver.ensure_agent_management_system_tools(
                can_manage_agents=config.policy.get("can_manage_agents", True)
            )
        )
        system_tools.extend(
            await tool_resolver.ensure_connector_system_tools(
                can_manage_integrations=config.policy.get("can_manage_integrations", True)
            )
        )
        system_tools.extend(
            await tool_resolver.ensure_memory_system_tools(
                can_manage_memory=config.policy.get("can_manage_memory", True)
            )
        )
        system_tools.extend(
            await tool_resolver.ensure_graph_system_tools(
                can_read_graph_context=_graph_context_tools_allowed(config.policy)
            )
        )
        system_tools.extend(
            await tool_resolver.ensure_execution_system_tools(
                can_inspect_executions=config.policy.get("can_inspect_executions", True)
            )
        )
        system_tools.extend(
            await tool_resolver.ensure_command_system_tools(
                can_run_commands=config.policy.get("can_run_commands", True)
            )
        )
        computer_use_tools = await self.list_visible_computer_use_tools() if config.policy.get("enable_computer_use",
                                                                                               True) else []
        agent = AgentDefinition(
            id=config.agent_id or str(uuid4()),
            name=config.agent_name.strip(),
            description=config.agent_description,
            instructions=config.agent_instructions.strip(),
            model_profile_id=model_profile.id,
            tool_ids=[*[tool.id for tool in system_tools], *[tool.id for tool in computer_use_tools]],
            graph_context=_agent_graph_context_settings(config.policy),
            metadata=dict(config.agent_metadata),
        )
        saved_agent = await self.context.agent_repo.save(agent)

        workflow_id = config.workflow_id or str(uuid4())
        profile_id = config.profile_id or str(uuid4())
        monitor_conversation = await self._ensure_monitor_conversation(profile_id=profile_id)
        node_id = f"{workflow_id}-node"
        workflow = WorkflowDefinition(
            id=workflow_id,
            name=config.workflow_name,
            description=config.workflow_description,
            entrypoint=node_id,
            nodes=[
                WorkflowNodeDefinition(
                    id=node_id,
                    name=f"{saved_agent.name} Node",
                    node_type=NodeType.AGENT,
                    agent_id=saved_agent.id,
                )
            ],
            agent_definitions=[saved_agent],
            metadata=dict(config.workflow_metadata),
        )
        saved_workflow = await self.context.workflow_repo.save(workflow)

        profile_metadata = self._profile_metadata_with_monitoring_defaults(
            config.profile_metadata,
            approval_conversation_id=monitor_conversation.id,
        )
        profile = MainAgentProfile(
            id=profile_id,
            name=config.profile_name,
            description=config.profile_description,
            agent_id=saved_agent.id,
            default_workflow_id=saved_workflow.id,
            default_model_profile_id=model_profile.id,
            policy=dict(config.policy),
            metadata=profile_metadata,
        )
        return await self.context.main_agent_profile_repo.save(profile)

    async def _ensure_monitor_conversation(self, *, profile_id: str) -> Conversation:
        conversation_id = f"{profile_id}-monitor"
        conversation = Conversation(
            id=conversation_id,
            title="Main Agent Monitor",
            main_agent_profile_id=profile_id,
            channel_type=ConversationChannelType.API,
            metadata={
                "source": "main_agent_setup",
                "purpose": "main_agent_monitor_notifications",
                "monitor_inbox": True,
                "delivery": {
                    "primary": "conversation",
                    "external_channels": "Use workflow/profile monitoring metadata to route to a linked chat channel.",
                },
            },
        )
        repo = self.context.conversation_repo
        if hasattr(repo, "save"):
            return await repo.save(conversation)
        existing = await repo.get(conversation_id)
        return existing if existing is not None else await repo.create(conversation)

    def _profile_metadata_with_monitoring_defaults(
            self,
            metadata: dict[str, Any],
            *,
            approval_conversation_id: str,
    ) -> dict[str, Any]:
        updated = dict(metadata)
        monitoring = dict(updated.get("main_agent_monitoring") or {})
        monitoring.setdefault("enabled", True)
        monitoring.setdefault("approval_conversation_id", approval_conversation_id)
        monitoring.setdefault("route_improvement_proposals_to_approval", True)
        monitoring.setdefault("route_steering_requests_to_approval", True)
        monitoring.setdefault("notification_routes", ["conversation"])
        monitoring.setdefault(
            "human_prompt_policy",
            {
                "prompt_for_findings": True,
                "prompt_for_improvement_proposals": True,
                "prompt_for_supervisor_steering": True,
                "never_auto_approve_high_risk": True,
                "never_auto_approve_repo_write": True,
            },
        )
        updated["main_agent_monitoring"] = monitoring
        return updated

    async def update_active_main_agent_profile(
            self,
            *,
            default_model_profile_id: str | None = None,
            name: str | None = None,
            description: str | None = None,
    ) -> MainAgentProfile:
        profile = await self.require_active_main_agent_profile()
        patch: dict[str, Any] = {}

        if name is not None:
            normalized_name = name.strip()
            if not normalized_name:
                raise MainAgentSetupInvalidError("Main-agent profile name cannot be empty.")
            patch["name"] = normalized_name

        if description is not None:
            patch["description"] = description.strip() or None

        if default_model_profile_id is not None:
            normalized_model_profile_id = default_model_profile_id.strip()
            if not normalized_model_profile_id:
                raise MainAgentModelProfileRequiredError(
                    "Main-agent model changes require a valid model profile id."
                )
            model_profile = await self.context.model_profile_repo.get(normalized_model_profile_id)
            if model_profile is None:
                raise MainAgentModelProfileRequiredError(
                    f"Model profile '{normalized_model_profile_id}' was not found for the active main agent."
                )
            patch["default_model_profile_id"] = model_profile.id

        if patch:
            updated = await self.context.main_agent_profile_repo.update(profile.id, patch)
            if updated is None:
                raise MainAgentSetupRequiredError(
                    "Main-agent setup has not been completed. Configure the first main agent before using conversations."
                )
            profile = updated

        if "default_model_profile_id" in patch:
            await self._sync_active_main_agent_model(profile)

        return profile

    async def sync_active_main_agent_instructions(self, instructions: str) -> AgentDefinition:
        normalized = instructions.strip()
        if not normalized:
            raise MainAgentSetupInvalidError("Main-agent instructions cannot be empty.")

        profile = await self.require_active_main_agent_profile()
        agent = await self.context.agent_repo.get(profile.agent_id)
        if agent is None:
            raise MainAgentSetupRequiredError(
                f"Active main-agent profile references missing agent '{profile.agent_id}'."
            )

        updated_agent = agent.model_copy(update={"instructions": normalized, "system_prompt": normalized})
        saved_agent = await self.context.agent_repo.save(updated_agent)

        workflow = await self.context.workflow_repo.get(profile.default_workflow_id)
        if workflow is not None:
            agent_definitions = [
                saved_agent if item.id == saved_agent.id else item
                for item in workflow.agent_definitions
            ]
            await self.context.workflow_repo.save(workflow.model_copy(update={"agent_definitions": agent_definitions}))

        return saved_agent

    async def sync_main_agent_tool_access(self, profile_id: str | None = None) -> MainAgentProfile | None:
        profile = await self.context.main_agent_profile_repo.get(
            profile_id) if profile_id else await self.get_active_main_agent_profile()
        if profile is None:
            return None
        agent = await self.context.agent_repo.get(profile.agent_id)
        if agent is None:
            return profile
        tool_resolver = AgentToolResolver(self.context)
        system_tools = await tool_resolver.ensure_workflow_system_tools(
            can_trigger_workflows=profile.policy.get("can_trigger_workflows", True)
        )
        system_tools.extend(
            await tool_resolver.ensure_goal_system_tools(
                can_manage_goals=profile.policy.get("can_manage_goals", True)
            )
        )
        system_tools.extend(await tool_resolver.ensure_default_main_agent_speech_tools())
        system_tools.extend(
            await tool_resolver.ensure_tool_management_system_tools(
                can_manage_tools=profile.policy.get("can_manage_tools", True)
            )
        )
        system_tools.extend(
            await tool_resolver.ensure_agent_management_system_tools(
                can_manage_agents=profile.policy.get("can_manage_agents", True)
            )
        )
        system_tools.extend(
            await tool_resolver.ensure_connector_system_tools(
                can_manage_integrations=profile.policy.get("can_manage_integrations", True)
            )
        )
        system_tools.extend(
            await tool_resolver.ensure_memory_system_tools(
                can_manage_memory=profile.policy.get("can_manage_memory", True)
            )
        )
        system_tools.extend(
            await tool_resolver.ensure_graph_system_tools(
                can_read_graph_context=_graph_context_tools_allowed(profile.policy)
            )
        )
        system_tools.extend(
            await tool_resolver.ensure_execution_system_tools(
                can_inspect_executions=profile.policy.get("can_inspect_executions", True)
            )
        )
        system_tools.extend(
            await tool_resolver.ensure_command_system_tools(
                can_run_commands=profile.policy.get("can_run_commands", True)
            )
        )
        visible_computer_use_tools = await self.list_visible_computer_use_tools() if profile.policy.get(
            "enable_computer_use", True) else []
        system_ids = {tool.id for tool in system_tools}
        visible_ids = {tool.id for tool in visible_computer_use_tools}
        preserved_ids = [
            tool_id
            for tool_id in agent.tool_ids
            if not tool_id.startswith("mcp:computer-use-")
               and not tool_id.startswith(LEGACY_MAIN_AGENT_INTERNAL_TOOL_PREFIX)
               and tool_id not in system_ids
        ]
        updated_agent = agent.model_copy(
            update={
                "tool_ids": [*preserved_ids, *sorted(system_ids), *sorted(visible_ids)],
                "graph_context": _agent_graph_context_settings(profile.policy),
            }
        )
        await self.context.agent_repo.save(updated_agent)
        return profile

    async def ensure_main_agent_tool_access_current(self, profile: MainAgentProfile) -> MainAgentProfile:
        agent = await self.context.agent_repo.get(profile.agent_id)
        if agent is None:
            return profile

        expected_ids = set(AgentToolResolver(self.context).main_agent_default_tool_ids(profile.policy))
        if expected_ids.issubset(set(agent.tool_ids)):
            return profile

        # Persisted profiles can outlive newly added system tools, and startup reconciliation may
        # have failed before a conversation begins. Repair only when the policy-derived grant is stale.
        return await self.sync_main_agent_tool_access(profile.id) or profile

    async def create_provider_and_model_profile(
            self,
            *,
            provider_id: str,
            provider_name: str,
            provider_type: ModelProviderType,
            profile_name: str,
            model_name: str,
            base_url: str | None = None,
            api_key: str | None = None,
            temperature: float | None = 0.2,
            max_tokens: int | None = 400,
            supports_structured_output: bool = False,
            supports_vision: bool = False,
            supports_streaming: bool = True,
            parameters: dict[str, Any] | None = None,
    ) -> ModelProfileDefinition:
        provider = await self.context.model_provider_repo.get(provider_id)
        if provider is None:
            provider = await self.context.model_provider_repo.save(
                ModelProviderDefinition(
                    id=provider_id,
                    name=provider_name,
                    provider_type=provider_type,
                    endpoint={"base_url": base_url} if base_url else None,
                )
            )
        profile = ModelProfileDefinition(
            id=str(uuid4()),
            name=profile_name,
            provider=provider.id,
            model=model_name,
            base_url=base_url,
            api_key_ref=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            supports_structured_output=supports_structured_output,
            supports_vision=supports_vision,
            supports_streaming=supports_streaming,
            parameters=parameters or {},
        )
        return await self.context.model_profile_repo.save(profile)

    async def _create_model_profile_from_bootstrap(self, bootstrap: MainAgentBootstrapConfig) -> ModelProfileDefinition:
        if bootstrap.existing_model_profile_id:
            profile = await self.context.model_profile_repo.get(bootstrap.existing_model_profile_id)
            if profile is None:
                raise MainAgentModelProfileRequiredError(
                    f"Configured MAIN_AGENT_BOOTSTRAP_EXISTING_MODEL_PROFILE_ID "
                    f"'{bootstrap.existing_model_profile_id}' was not found."
                )
            return profile
        if not bootstrap.provider_family:
            raise MainAgentSetupInvalidError(
                "MAIN_AGENT_BOOTSTRAP_PROVIDER_FAMILY is required when no model profiles exist and "
                "MAIN_AGENT_BOOTSTRAP_EXISTING_MODEL_PROFILE_ID is not set."
            )
        if not bootstrap.model_name:
            raise MainAgentSetupInvalidError(
                "MAIN_AGENT_BOOTSTRAP_MODEL_NAME is required when creating a model profile from env bootstrap."
            )
        provider_key = bootstrap.provider_family.strip().lower()
        try:
            provider_name, provider_type, default_base_url, requires_api_key = self._provider_defaults(provider_key)
        except KeyError as exc:
            raise MainAgentSetupInvalidError(
                "MAIN_AGENT_BOOTSTRAP_PROVIDER_FAMILY must be one of: "
                "openai, anthropic, google, xai, deepseek, qwen, ollama, openai_compatible, openai_codex."
            ) from exc
        effective_provider_id = provider_key
        effective_provider_type = provider_type
        effective_base_url = bootstrap.base_url or default_base_url
        effective_provider_name = bootstrap.provider_name or provider_name
        if provider_key == "xai":
            effective_provider_id = "openai_compatible"
            effective_provider_type = ModelProviderType.OPENAI_COMPATIBLE
            effective_base_url = bootstrap.base_url or "https://api.x.ai/v1"
        if requires_api_key and not bootstrap.api_key:
            raise MainAgentSetupInvalidError(
                f"MAIN_AGENT_BOOTSTRAP_API_KEY is required for provider family '{provider_key}'."
            )
        parameters: dict[str, Any] = {}
        if provider_key == "openai_codex":
            parameters["auth_mode"] = "api" if bootstrap.api_key else "chatgpt"
            parameters["codex_cli_timeout_seconds"] = 1800
        return await self.create_provider_and_model_profile(
            provider_id=effective_provider_id,
            provider_name=effective_provider_name,
            provider_type=effective_provider_type,
            profile_name=bootstrap.profile_name or f"{effective_provider_name} Main",
            model_name=bootstrap.model_name,
            base_url=effective_base_url,
            api_key=bootstrap.api_key,
            temperature=0.2 if bootstrap.temperature is None else bootstrap.temperature,
            max_tokens=400 if bootstrap.max_tokens is None else bootstrap.max_tokens,
            parameters=parameters,
        )

    def _build_main_agent_config_from_bootstrap(
            self,
            bootstrap: MainAgentBootstrapConfig,
            profiles: list[ModelProfileDefinition],
    ) -> MainAgentSetupConfig:
        model_profile_id = bootstrap.existing_model_profile_id
        if model_profile_id is None:
            if len(profiles) != 1:
                raise MainAgentSetupInvalidError(
                    "MAIN_AGENT_BOOTSTRAP_EXISTING_MODEL_PROFILE_ID is required when multiple model profiles exist."
                )
            model_profile_id = profiles[0].id
        return MainAgentSetupConfig(
            agent_name=bootstrap.agent_name,
            agent_description=bootstrap.agent_description,
            agent_instructions=bootstrap.agent_instructions,
            model_profile_id=model_profile_id,
            workflow_name=bootstrap.workflow_name,
            workflow_description=bootstrap.workflow_description,
            profile_name=bootstrap.agent_name,
            profile_description=bootstrap.agent_description,
            policy={
                "can_answer_directly": True,
                "can_trigger_workflows": bootstrap.can_trigger_workflows,
                "can_create_workflows": bootstrap.can_create_workflows,
                "can_update_workflows": bootstrap.can_update_workflows,
                "can_manage_integrations": True,
                "can_inspect_executions": True,
                "can_run_commands": True,
                "require_approval_for_mutations": bootstrap.require_approval_for_mutations,
            },
            profile_metadata={
                "system": True,
                "setup_mode": "env",
                "bootstrap": {
                    "provider_family": bootstrap.provider_family,
                    "existing_model_profile_id": bootstrap.existing_model_profile_id,
                },
            },
        )

    async def _assert_profile_references_are_valid(self, profile: MainAgentProfile) -> None:
        agent = await self.context.agent_repo.get(profile.agent_id)
        if agent is None:
            raise MainAgentSetupInvalidError(
                f"Main-agent profile '{profile.id}' references missing agent '{profile.agent_id}'."
            )
        workflow = await self.context.workflow_repo.get(profile.default_workflow_id)
        if workflow is None:
            raise MainAgentSetupInvalidError(
                f"Main-agent profile '{profile.id}' references missing workflow '{profile.default_workflow_id}'."
            )

    async def _sync_active_main_agent_model(self, profile: MainAgentProfile) -> None:
        if not profile.default_model_profile_id:
            raise MainAgentModelProfileRequiredError(
                "The active main agent must reference a valid default model profile.")

        agent = await self.context.agent_repo.get(profile.agent_id)
        if agent is None:
            raise MainAgentSetupInvalidError(
                f"Main-agent profile '{profile.id}' references missing agent '{profile.agent_id}'."
            )
        if agent.model_profile_id != profile.default_model_profile_id:
            await self.context.agent_repo.update(agent.id, {"model_profile_id": profile.default_model_profile_id})

        workflow = await self.context.workflow_repo.get(profile.default_workflow_id)
        if workflow is None:
            raise MainAgentSetupInvalidError(
                f"Main-agent profile '{profile.id}' references missing workflow '{profile.default_workflow_id}'."
            )

        updated_agent_definitions = []
        found_agent = False
        for item in workflow.agent_definitions:
            if item.id == agent.id:
                found_agent = True
                if item.model_profile_id != profile.default_model_profile_id:
                    updated_agent_definitions.append(
                        item.model_copy(update={"model_profile_id": profile.default_model_profile_id})
                    )
                else:
                    updated_agent_definitions.append(item)
            else:
                updated_agent_definitions.append(item)

        if not found_agent:
            raise MainAgentSetupInvalidError(
                f"Main workflow '{workflow.id}' does not contain the linked main agent '{agent.id}'."
            )

        if updated_agent_definitions != workflow.agent_definitions:
            await self.context.workflow_repo.update(
                workflow.id,
                {"agent_definitions": [item.model_dump(mode="json") for item in updated_agent_definitions]},
            )

    def _prompt_for_main_agent_config(
            self,
            profiles: list[ModelProfileDefinition],
            *,
            default_agent_instructions: str | None = None,
    ) -> MainAgentSetupConfig:
        print(
            "\nNo configured main agent was found for this backend.\n"
            "A one-time setup is required to create the first main agent.\n"
        )
        print("Available model profiles:")
        for index, profile in enumerate(profiles, start=1):
            print(f"  {index}. {profile.name} ({profile.model}) [{profile.provider}]")

        agent_name = self._prompt_text("Main agent display name", default="Main Agent")
        agent_description = self._prompt_text(
            "Main agent description",
            default="Default conversational orchestrator for this deployment.",
        )
        agent_instructions = self._prompt_multiline(
            "Main agent core instructions",
            default=default_agent_instructions,
        )
        selected_profile = self._prompt_profile_selection(profiles)
        can_trigger = self._prompt_bool("Allow this main agent to trigger workflows?", default=True)
        can_create = self._prompt_bool("Allow this main agent to create workflows?", default=True)
        can_update = self._prompt_bool("Allow this main agent to update workflows?", default=True)
        require_approval = self._prompt_bool("Require human approval for workflow mutations?", default=True)
        workflow_name = self._prompt_text("Default workflow name", default="Main Workflow")
        workflow_description = self._prompt_text(
            "Default workflow description",
            default="Default workflow for main-agent orchestration.",
        )
        chat_access = self._prompt_chat_access_path()

        config = MainAgentSetupConfig(
            agent_name=agent_name,
            agent_description=agent_description,
            agent_instructions=agent_instructions,
            model_profile_id=selected_profile.id,
            workflow_name=workflow_name,
            workflow_description=workflow_description,
            profile_name=agent_name,
            profile_description=agent_description,
            policy={
                "can_answer_directly": True,
                "can_trigger_workflows": can_trigger,
                "can_create_workflows": can_create,
                "can_update_workflows": can_update,
                "can_manage_integrations": True,
                "can_inspect_executions": True,
                "require_approval_for_mutations": require_approval,
            },
            profile_metadata={
                "system": True,
                "setup_mode": "interactive",
                "operator_chat_access": chat_access,
            },
        )

        print("\nMain-agent setup summary:")
        print(f"  name: {config.agent_name}")
        print(f"  model profile: {selected_profile.name} ({selected_profile.model})")
        print(f"  workflow: {config.workflow_name}")
        print(f"  can_trigger_workflows: {config.policy['can_trigger_workflows']}")
        print(f"  can_create_workflows: {config.policy['can_create_workflows']}")
        print(f"  can_update_workflows: {config.policy['can_update_workflows']}")
        print(f"  require_approval_for_mutations: {config.policy['require_approval_for_mutations']}")
        if chat_access["mode"] == "direct_cli":
            print("  chat access: direct CLI chat (`python -m app.cli chat-main-agent`)")
        else:
            print(f"  chat access: prepare {chat_access['channel']} bot setup")
            print(f"  next step: python -m app.cli setup-chat-channel {chat_access['channel'].lower()}")
        confirmed = self._prompt_bool("Create this main agent now?", default=True)
        if not confirmed:
            raise MainAgentSetupRequiredError(
                "Main-agent setup was cancelled. Startup cannot continue until the first main agent is configured."
            )
        return config

    async def _prompt_and_create_model_profile(self) -> ModelProfileDefinition:
        print(
            "\nNo usable model profiles were found.\n"
            "A provider and model profile must be configured before the first main agent can be created.\n"
        )
        provider_key = self._prompt_provider_family()
        provider_name, provider_type, base_url, requires_api_key = self._provider_defaults(provider_key)
        if provider_key == "openai_compatible":
            provider_name = self._prompt_text("Provider display name", default=provider_name)
            base_url = self._prompt_text("Provider base URL", default=base_url or "http://localhost:8001/v1")
        elif provider_key == "xai":
            provider_name = "xAI"
        elif provider_key == "ollama":
            base_url = self._prompt_text(
                "Ollama base URL",
                default=base_url or "http://host.docker.internal:11434",
            )

        profile_name = self._prompt_text("Model profile display name", default=f"{provider_name} Main")
        model_name = self._prompt_text("Default model name")
        api_key = None
        if requires_api_key:
            api_key = self._prompt_text("API key")
        temperature = self._prompt_optional_float("Temperature", default=0.2)
        max_tokens = self._prompt_optional_int("Max tokens", default=400)

        provider_id = provider_key
        effective_provider_type = provider_type
        effective_base_url = base_url
        if provider_key == "xai":
            provider_id = "openai_compatible"
            effective_provider_type = ModelProviderType.OPENAI_COMPATIBLE
            effective_base_url = "https://api.x.ai/v1"

        profile = await self.create_provider_and_model_profile(
            provider_id=provider_id,
            provider_name=provider_name,
            provider_type=effective_provider_type,
            profile_name=profile_name,
            model_name=model_name,
            base_url=effective_base_url,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        print(
            "\nModel profile created.\n"
            f"  profile_id: {profile.id}\n"
            f"  provider: {profile.provider}\n"
            f"  model: {profile.model}\n"
        )
        return profile

    def _prompt_provider_family(self) -> str:
        options = [
            ("openai", "OpenAI (ChatGPT)"),
            ("openai_codex", "OpenAI Codex (OAuth)"),
            ("anthropic", "Anthropic (Claude)"),
            ("google", "Google Gemini"),
            ("xai", "xAI / Grok"),
            ("deepseek", "DeepSeek"),
            ("qwen", "Qwen / DashScope"),
            ("ollama", "Ollama / local models"),
            ("openai_compatible", "Custom OpenAI-compatible endpoint"),
        ]
        print("Supported provider families:")
        for index, (_, label) in enumerate(options, start=1):
            print(f"  {index}. {label}")
        while True:
            raw = input(f"Select provider family [1-{len(options)}] [1]: ").strip()
            if not raw:
                return options[0][0]
            try:
                selected = int(raw)
            except ValueError:
                print("Please enter a valid number.")
                continue
            if 1 <= selected <= len(options):
                return options[selected - 1][0]
            print("Selection out of range.")

    def _provider_defaults(self, provider_key: str) -> tuple[str, ModelProviderType, str | None, bool]:
        backend_run_mode = os.getenv("AGENCY_BACKEND_RUN_MODE") or get_settings().agency_backend_run_mode
        local_ollama_url = (
            "http://localhost:11434"
            if backend_run_mode == "host"
            else "http://host.docker.internal:11434"
        )
        mapping: dict[str, tuple[str, ModelProviderType, str | None, bool]] = {
            "openai": ("OpenAI", ModelProviderType.OPENAI, None, True),
            "anthropic": ("Anthropic", ModelProviderType.ANTHROPIC, "https://api.anthropic.com", True),
            "google": ("Google Gemini", ModelProviderType.GOOGLE, "https://generativelanguage.googleapis.com", True),
            "xai": ("xAI", ModelProviderType.OPENAI_COMPATIBLE, "https://api.x.ai/v1", True),
            "deepseek": ("DeepSeek", ModelProviderType.DEEPSEEK, "https://api.deepseek.com", True),
            "qwen": ("Qwen", ModelProviderType.QWEN, "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", True),
            "ollama": ("Ollama", ModelProviderType.OLLAMA, local_ollama_url, False),
            "openai_compatible": ("OpenAI-Compatible", ModelProviderType.OPENAI_COMPATIBLE, "http://localhost:8001/v1",
                                  True),
            "openai_codex": ("OpenAI Codex", ModelProviderType.OPENAI_CODEX, "https://api.openai.com/v1", False),
        }
        return mapping[provider_key]

    def _prompt_text(self, label: str, *, default: str | None = None) -> str:
        suffix = f" [{default}]" if default is not None else ""
        raw = input(f"{label}{suffix}: ").strip()
        if raw:
            return raw
        if default is not None:
            return default
        raise MainAgentSetupInvalidError(f"{label} is required.")

    def _prompt_chat_access_path(self) -> dict[str, Any]:
        print(
            "\nChoose how you want to chat with the main agent after setup.\n"
            "You can keep using the backend CLI directly, or prepare a Discord or Telegram bot later.\n"
        )
        options = [
            {
                "mode": "direct_cli",
                "label": "Direct CLI chat (recommended)",
                "channel": None,
                "guidance": "Use the backend CLI to chat with the agent immediately after setup.",
            },
            {
                "mode": "discord_bot",
                "label": "Discord bot",
                "channel": "Discord",
                "guidance": "Prepare Discord webhook credentials so the same assistant can be reached in Discord.",
            },
            {
                "mode": "telegram_bot",
                "label": "Telegram bot",
                "channel": "Telegram",
                "guidance": "Prepare Telegram bot credentials so the same assistant can be reached in Telegram.",
            },
        ]
        for index, option in enumerate(options, start=1):
            print(f"  {index}. {option['label']}")
        while True:
            raw = input(f"Select chat access path [1-{len(options)}] [1]: ").strip()
            if not raw:
                selection = options[0]
                break
            try:
                selected = int(raw)
            except ValueError:
                print("Please enter a valid number.")
                continue
            if 1 <= selected <= len(options):
                selection = options[selected - 1]
                break
            print("Selection out of range.")
        return {
            "mode": selection["mode"],
            "channel": selection["channel"],
            "guidance": selection["guidance"],
        }

    def _prompt_multiline(self, label: str, *, default: str | None = None) -> str:
        print(f"{label}:")
        print("  Enter text. Finish with an empty line.")
        lines: list[str] = []
        while True:
            line = input()
            if not line.strip():
                break
            lines.append(line)
        if lines:
            return "\n".join(lines).strip()
        if default is not None:
            return default
        raise MainAgentSetupInvalidError(f"{label} is required.")

    def _prompt_bool(self, label: str, *, default: bool) -> bool:
        suffix = "Y/n" if default else "y/N"
        while True:
            raw = input(f"{label} [{suffix}]: ").strip().lower()
            if not raw:
                return default
            if raw in {"y", "yes"}:
                return True
            if raw in {"n", "no"}:
                return False
            print("Please answer yes or no.")

    def _prompt_profile_selection(self, profiles: list[ModelProfileDefinition]) -> ModelProfileDefinition:
        default_index = 1
        while True:
            raw = input(f"Select default model profile [1-{len(profiles)}] [{default_index}]: ").strip()
            if not raw:
                return profiles[default_index - 1]
            try:
                selected = int(raw)
            except ValueError:
                print("Please enter a valid number.")
                continue
            if 1 <= selected <= len(profiles):
                return profiles[selected - 1]
            print("Selection out of range.")

    def _prompt_optional_float(self, label: str, *, default: float | None = None) -> float | None:
        suffix = f" [{default}]" if default is not None else ""
        while True:
            raw = input(f"{label}{suffix}: ").strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError:
                print("Please enter a valid number.")

    def _prompt_optional_int(self, label: str, *, default: int | None = None) -> int | None:
        suffix = f" [{default}]" if default is not None else ""
        while True:
            raw = input(f"{label}{suffix}: ").strip()
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                print("Please enter a valid integer.")


__all__ = [
    "MainAgentModelProfileRequiredError",
    "MainAgentSetupConfig",
    "MainAgentSetupError",
    "MainAgentSetupInvalidError",
    "MainAgentSetupRequiredError",
    "MainAgentSetupService",
]
