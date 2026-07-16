"""Policy decisions that constrain main-agent autonomy in conversations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.core.config import Settings, get_settings
from app.domain import Conversation, ConversationMessageType, ConversationRole, ToolDefinition, WorkflowDefinition

if TYPE_CHECKING:
    from app.api.context import ApiContext


@dataclass(frozen=True, slots=True)
class MainAgentPolicyDecision:
    allowed: bool
    reason: str | None = None
    code: str | None = None

    @classmethod
    def allow(cls) -> "MainAgentPolicyDecision":
        return cls(allowed=True)

    @classmethod
    def deny(cls, reason: str, *, code: str) -> "MainAgentPolicyDecision":
        return cls(allowed=False, reason=reason, code=code)


class MainAgentPolicyService:
    """Central policy surface for main-agent conversation autonomy."""

    _MONITORING_LEVELS = {"minimal", "standard", "strict"}
    _TRUSTED_FIRST_PARTY_CHANNELS = {"api", "web"}
    _DENY_METADATA_KEYS = {
        "deny_main_agent",
        "denied_to_main_agent",
        "hidden_from_main_agent",
        "hide_from_main_agent",
        "main_agent_denied",
    }
    _DENY_TAGS = {
        "deny_main_agent",
        "denied_to_main_agent",
        "hidden_from_main_agent",
        "hide_from_main_agent",
    }

    def __init__(self, context: ApiContext, *, settings: Settings | None = None) -> None:
        self.context = context
        self.settings = settings or get_settings()

    async def check_external_channel_message_budget(self, conversation_id: str) -> MainAgentPolicyDecision:
        conversation = await self.context.conversation_repo.get(conversation_id)
        if conversation is None or not self.is_external_channel(conversation):
            return MainAgentPolicyDecision.allow()

        budget = self.settings.main_agent_external_channel_daily_message_budget
        if budget < 0:
            return MainAgentPolicyDecision.allow()

        today = datetime.now(timezone.utc).date()
        messages = await self.context.conversation_message_repo.list_by_conversation(conversation_id)
        user_message_count = sum(
            1
            for message in messages
            if message.role == ConversationRole.USER
            and message.message_type == ConversationMessageType.USER_TEXT
            and message.created_at.astimezone(timezone.utc).date() == today
        )
        if user_message_count > budget:
            return MainAgentPolicyDecision.deny(
                "This external channel has reached its main-agent request budget for today.",
                code="external_channel_budget_exceeded",
            )
        return MainAgentPolicyDecision.allow()

    def is_external_channel(self, conversation: Conversation) -> bool:
        return conversation.channel_type.value not in self._TRUSTED_FIRST_PARTY_CHANNELS

    def is_trusted_conversation(self, conversation: Conversation | None) -> bool:
        if conversation is None:
            return False
        if conversation.created_by_user_id:
            return True
        return conversation.channel_type.value in self._TRUSTED_FIRST_PARTY_CHANNELS

    async def check_trusted_conversation(
            self,
            conversation_id: str,
            *,
            reason: str,
            code: str,
    ) -> MainAgentPolicyDecision:
        conversation = await self.context.conversation_repo.get(conversation_id)
        if self.is_trusted_conversation(conversation):
            return MainAgentPolicyDecision.allow()
        return MainAgentPolicyDecision.deny(reason, code=code)

    def workflow_is_visible(self, workflow: WorkflowDefinition) -> bool:
        return (
                not self._metadata_denies(workflow.metadata)
                and (
                        workflow.metadata.get("visible_to_agent") is True
                        or workflow.metadata.get("visible_to_main_agent") is True
                )
        )

    def workflow_is_mutable(self, workflow: WorkflowDefinition) -> bool:
        return (
                self.settings.main_agent_workflow_mutation_enabled
                and self.workflow_is_visible(workflow)
                and workflow.metadata.get("mutable_by_main_agent") is True
                and not self._metadata_denies(workflow.metadata)
        )

    def workflow_requires_execution_approval(self, workflow: WorkflowDefinition) -> bool:
        return workflow.metadata.get("protected_execution") is True

    def workflow_is_monitorable_by_main_agent(self, workflow: WorkflowDefinition) -> bool:
        return self.workflow_monitoring_level(workflow) != "off"

    def workflow_monitoring_level(self, workflow: WorkflowDefinition) -> str:
        monitoring = workflow.metadata.get("main_agent_monitoring")
        if not self.workflow_is_visible(workflow):
            return "off"
        if isinstance(monitoring, dict):
            if monitoring.get("enabled") is False:
                return "off"
            level = str(monitoring.get("level") or "").strip().lower()
            if level == "off":
                return "off"
            if level in self._MONITORING_LEVELS:
                return level
            if monitoring.get("enabled") is True:
                return "standard"
        return "standard" if self.settings.main_agent_workflow_monitor_default_enabled else "off"

    def workflow_monitoring_summary(self, workflow: WorkflowDefinition) -> dict[str, Any]:
        monitoring = workflow.metadata.get("main_agent_monitoring")
        monitoring = monitoring if isinstance(monitoring, dict) else {}
        level = self.workflow_monitoring_level(workflow)
        return {
            "enabled": level != "off",
            "level": level,
            "exempted": level == "off" and monitoring.get("enabled") is False,
            "reason": monitoring.get("reason"),
            "visible_to_main_agent": self.workflow_is_visible(workflow),
            "mutable_by_main_agent": self.workflow_is_mutable(workflow),
            "default_enabled": self.settings.main_agent_workflow_monitor_default_enabled,
        }

    def check_workflow_visibility(self, workflow: WorkflowDefinition) -> MainAgentPolicyDecision:
        if self.workflow_is_visible(workflow):
            return MainAgentPolicyDecision.allow()
        return MainAgentPolicyDecision.deny(
            f"I cannot access workflow '{workflow.name}'.",
            code="workflow_hidden",
        )

    def check_workflow_mutation_enabled(self) -> MainAgentPolicyDecision:
        if self.settings.main_agent_workflow_mutation_enabled:
            return MainAgentPolicyDecision.allow()
        return MainAgentPolicyDecision.deny(
            "Main-agent workflow mutation is disabled by policy.",
            code="workflow_mutation_disabled",
        )

    def check_tool_mutation_enabled(self) -> MainAgentPolicyDecision:
        if self.settings.main_agent_tool_mutation_enabled:
            return MainAgentPolicyDecision.allow()
        return MainAgentPolicyDecision.deny(
            "Main-agent tool mutation is disabled by policy.",
            code="tool_mutation_disabled",
        )

    async def check_workflow_execution_channel(self, conversation_id: str) -> MainAgentPolicyDecision:
        return await self.check_trusted_conversation(
            conversation_id,
            reason="This channel is not allowed to launch workflows without a trusted mapped identity.",
            code="untrusted_workflow_execution_channel",
        )

    async def check_workflow_mutation_channel(self, conversation_id: str) -> MainAgentPolicyDecision:
        return await self.check_trusted_conversation(
            conversation_id,
            reason="This channel is not allowed to create or update workflows without a trusted mapped identity.",
            code="untrusted_workflow_mutation_channel",
        )

    async def check_tool_execution_channel(self, conversation_id: str) -> MainAgentPolicyDecision:
        return await self.check_trusted_conversation(
            conversation_id,
            reason="This channel is not allowed to run approval-gated tools without a trusted mapped identity.",
            code="untrusted_tool_execution_channel",
        )

    async def check_tool_mutation_channel(self, conversation_id: str) -> MainAgentPolicyDecision:
        return await self.check_trusted_conversation(
            conversation_id,
            reason="This channel is not allowed to create or update tools without a trusted mapped identity.",
            code="untrusted_tool_mutation_channel",
        )

    def tool_is_visible(self, tool: ToolDefinition) -> bool:
        metadata = self._tool_metadata(tool)
        if self._metadata_denies(metadata):
            return False
        return not any(tag in self._DENY_TAGS for tag in tool.tags)

    def tool_is_visible_to_user(self, tool: ToolDefinition, user_id: str | None) -> bool:
        """Apply optional per-user restrictions after the agent-level allowlist.

        User policy can only narrow visibility. Keeping this check after agent resolution
        prevents routing from turning user metadata into a capability-expansion mechanism.
        """
        if not self.tool_is_visible(tool):
            return False
        metadata = self._tool_metadata(tool)
        allowed = self._metadata_string_set(metadata, "allowed_user_ids")
        if allowed and user_id not in allowed:
            return False
        denied = self._metadata_string_set(metadata, "denied_user_ids")
        if user_id in denied:
            return False
        return True

    @staticmethod
    def _metadata_string_set(metadata: dict[str, Any], key: str) -> set[str]:
        values = metadata.get(key)
        if not isinstance(values, list):
            return set()
        return {value.strip() for value in values if isinstance(value, str) and value.strip()}

    def _tool_metadata(self, tool: ToolDefinition) -> dict[str, Any]:
        metadata = dict(tool.framework_hints.metadata)
        config = tool.implementation.config
        if isinstance(config, dict):
            policy = config.get("policy")
            if isinstance(policy, dict):
                metadata.update(policy)
        return metadata

    def _metadata_denies(self, metadata: dict[str, Any]) -> bool:
        return any(metadata.get(key) is True for key in self._DENY_METADATA_KEYS)
