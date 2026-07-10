"""OneCLI identity mapping and default rule-profile service.

The service keeps Agency-owned metadata for routing credentialed HTTP/tool
traffic through OneCLI without storing raw OneCLI agent tokens in normal API
payloads. Runtime enforcement and proxy URL construction live in integration and
tool layers; this module owns the persisted Agency-side mapping lifecycle.
"""

from __future__ import annotations

from typing import Any

from app.api.context import ApiContext
from app.core.config import get_settings
from app.domain import (
    OneCLIIdentityMapping,
    OneCLIIdentityMappingStatus,
    OneCLIRateLimitWindow,
    OneCLIRuleAction,
    OneCLIRuleProfile,
    OneCLIRuleTemplate,
)

DEFAULT_ONECLI_RULE_PROFILE_ID = "agency-default-user-rules"
DEFAULT_ONECLI_RULE_PROFILE_VERSION = 1


def get_default_onecli_rule_profile() -> OneCLIRuleProfile:
    return OneCLIRuleProfile(
        id=DEFAULT_ONECLI_RULE_PROFILE_ID,
        version=DEFAULT_ONECLI_RULE_PROFILE_VERSION,
        name="Agency Default User Rules",
        description=(
            "Baseline OneCLI gateway rules for new Agency user mappings. "
            "These rules are token-free templates for operator bootstrap in OneCLI."
        ),
        rules=[
            OneCLIRuleTemplate(
                id="block-gmail-message-delete",
                name="Block Gmail message deletion",
                description="Prevent agents from deleting Gmail messages.",
                host_pattern="gmail.googleapis.com",
                path_pattern="/gmail/v1/users/*/messages/*",
                method="DELETE",
                action=OneCLIRuleAction.BLOCK,
                category="blocked_endpoint",
            ),
            OneCLIRuleTemplate(
                id="block-microsoft-graph-message-delete",
                name="Block Microsoft Graph message deletion",
                description="Prevent agents from deleting Microsoft Graph mailbox messages.",
                host_pattern="graph.microsoft.com",
                path_pattern="/v1.0/*/messages/*",
                method="DELETE",
                action=OneCLIRuleAction.BLOCK,
                category="blocked_endpoint",
            ),
            OneCLIRuleTemplate(
                id="block-stripe-production-payment-mutation",
                name="Block Stripe payment mutations",
                description="Block production payment, refund, customer, and subscription mutations.",
                host_pattern="api.stripe.com",
                path_pattern="/v1/*",
                method="POST",
                action=OneCLIRuleAction.BLOCK,
                category="blocked_endpoint",
                notes="Use narrower per-workflow allow rules only after operator review.",
            ),
            OneCLIRuleTemplate(
                id="block-stripe-production-payment-delete",
                name="Block Stripe deletes",
                description="Block destructive Stripe delete calls.",
                host_pattern="api.stripe.com",
                path_pattern="/v1/*",
                method="DELETE",
                action=OneCLIRuleAction.BLOCK,
                category="blocked_endpoint",
            ),
            OneCLIRuleTemplate(
                id="block-github-repository-delete",
                name="Block GitHub repository deletion",
                description="Prevent agents from deleting GitHub repositories.",
                host_pattern="api.github.com",
                path_pattern="/repos/*",
                method="DELETE",
                action=OneCLIRuleAction.BLOCK,
                category="blocked_endpoint",
            ),
            OneCLIRuleTemplate(
                id="block-gitlab-project-delete",
                name="Block GitLab project deletion",
                description="Prevent agents from deleting GitLab projects.",
                host_pattern="gitlab.com",
                path_pattern="/api/v4/projects/*",
                method="DELETE",
                action=OneCLIRuleAction.BLOCK,
                category="blocked_endpoint",
            ),
            OneCLIRuleTemplate(
                id="block-google-iam-post-mutation",
                name="Block Google IAM POST mutation",
                description="Prevent agents from mutating Google IAM service accounts and policies with POST.",
                host_pattern="iam.googleapis.com",
                path_pattern="/v1/projects/*",
                method="POST",
                action=OneCLIRuleAction.BLOCK,
                category="blocked_endpoint",
            ),
            OneCLIRuleTemplate(
                id="block-google-iam-patch-mutation",
                name="Block Google IAM PATCH mutation",
                description="Prevent agents from updating Google IAM resources.",
                host_pattern="iam.googleapis.com",
                path_pattern="/v1/projects/*",
                method="PATCH",
                action=OneCLIRuleAction.BLOCK,
                category="blocked_endpoint",
            ),
            OneCLIRuleTemplate(
                id="block-google-iam-delete-mutation",
                name="Block Google IAM DELETE mutation",
                description="Prevent agents from deleting Google IAM resources.",
                host_pattern="iam.googleapis.com",
                path_pattern="/v1/projects/*",
                method="DELETE",
                action=OneCLIRuleAction.BLOCK,
                category="blocked_endpoint",
            ),
            OneCLIRuleTemplate(
                id="block-google-kms-post-mutation",
                name="Block Google Cloud KMS POST mutation",
                description="Prevent agents from mutating Google Cloud KMS key resources with POST.",
                host_pattern="cloudkms.googleapis.com",
                path_pattern="/v1/projects/*",
                method="POST",
                action=OneCLIRuleAction.BLOCK,
                category="blocked_endpoint",
            ),
            OneCLIRuleTemplate(
                id="block-google-kms-patch-mutation",
                name="Block Google Cloud KMS PATCH mutation",
                description="Prevent agents from updating Google Cloud KMS key resources.",
                host_pattern="cloudkms.googleapis.com",
                path_pattern="/v1/projects/*",
                method="PATCH",
                action=OneCLIRuleAction.BLOCK,
                category="blocked_endpoint",
            ),
            OneCLIRuleTemplate(
                id="block-google-kms-delete-mutation",
                name="Block Google Cloud KMS DELETE mutation",
                description="Prevent agents from deleting Google Cloud KMS key resources.",
                host_pattern="cloudkms.googleapis.com",
                path_pattern="/v1/projects/*",
                method="DELETE",
                action=OneCLIRuleAction.BLOCK,
                category="blocked_endpoint",
            ),
            OneCLIRuleTemplate(
                id="block-aws-iam-mutation",
                name="Block AWS IAM mutation",
                description="Prevent agents from mutating AWS IAM resources through the query API.",
                host_pattern="iam.amazonaws.com",
                path_pattern="/",
                method="POST",
                action=OneCLIRuleAction.BLOCK,
                category="blocked_endpoint",
            ),
            OneCLIRuleTemplate(
                id="rate-limit-slack-post-message",
                name="Rate limit Slack messages",
                description="Limit Slack message sends per agent.",
                host_pattern="slack.com",
                path_pattern="/api/chat.postMessage",
                method="POST",
                action=OneCLIRuleAction.RATE_LIMIT,
                rate_limit_count=10,
                rate_limit_window=OneCLIRateLimitWindow.HOUR,
                category="rate_limit",
            ),
            OneCLIRuleTemplate(
                id="rate-limit-gmail-send",
                name="Rate limit Gmail sends",
                description="Limit Gmail send attempts per agent.",
                host_pattern="gmail.googleapis.com",
                path_pattern="/gmail/v1/users/me/messages/send",
                method="POST",
                action=OneCLIRuleAction.RATE_LIMIT,
                rate_limit_count=10,
                rate_limit_window=OneCLIRateLimitWindow.HOUR,
                category="rate_limit",
            ),
            OneCLIRuleTemplate(
                id="rate-limit-github-write",
                name="Rate limit GitHub writes",
                description="Limit high-volume GitHub write calls per agent.",
                host_pattern="api.github.com",
                path_pattern="/repos/*",
                method="POST",
                action=OneCLIRuleAction.RATE_LIMIT,
                rate_limit_count=30,
                rate_limit_window=OneCLIRateLimitWindow.HOUR,
                category="rate_limit",
            ),
            OneCLIRuleTemplate(
                id="approval-gmail-send",
                name="Require approval for Gmail sends",
                description="Require human approval before agents send Gmail messages.",
                host_pattern="gmail.googleapis.com",
                path_pattern="/gmail/v1/users/me/messages/send",
                method="POST",
                action=OneCLIRuleAction.MANUAL_APPROVAL,
                default_enabled=False,
                category="manual_approval",
                notes="Enable after OneCLI manual-approval polling is connected to Agency approvals.",
            ),
        ],
    )


class OneCLIIdentityMappingService:
    def __init__(self, context: ApiContext):
        self.context = context

    def public_mapping(self, mapping: OneCLIIdentityMapping) -> dict[str, Any]:
        payload = mapping.model_dump(mode="json", exclude={"agent_token_secret_ref"})
        payload["agent_token_secret_ref_configured"] = bool(mapping.agent_token_secret_ref)
        return payload

    def default_rule_profile(self) -> OneCLIRuleProfile:
        return get_default_onecli_rule_profile()

    def public_default_rule_profile(self) -> dict[str, Any]:
        return self.default_rule_profile().model_dump(mode="json")

    def _with_default_rule_profile_metadata(self, payload: dict[str, Any]) -> dict[str, Any]:
        merged = dict(payload)
        metadata = dict(merged.get("metadata") or {})
        metadata.setdefault(
            "onecli_rule_profile",
            {
                "id": DEFAULT_ONECLI_RULE_PROFILE_ID,
                "version": DEFAULT_ONECLI_RULE_PROFILE_VERSION,
                "status": "pending_onecli_bootstrap",
                "rule_ids": [rule.id for rule in self.default_rule_profile().rules if rule.default_enabled],
            },
        )
        merged["metadata"] = metadata
        return merged

    def _audit_mapping(
            self,
            action: str,
            mapping: OneCLIIdentityMapping,
            *,
            actor_user_id: str | None = None,
            admin: bool = False,
            reason: str | None = None,
            credential_id: str | None = None,
    ) -> None:
        payload = dict(
            mapping_id=mapping.id,
            owner_user_id=mapping.owner_user_id,
            actor_user_id=actor_user_id,
            admin=admin,
            name=mapping.name,
            onecli_agent_id=mapping.onecli_agent_id,
            workflow_id=mapping.workflow_id,
            status=mapping.status.value,
            agent_token_secret_ref_configured=bool(mapping.agent_token_secret_ref),
        )
        if reason:
            payload["reason"] = reason
        if credential_id:
            payload["credential_id"] = credential_id
        self.context.runtime_operations.record_action(f"onecli.identity_mapping.{action}", **payload)

    async def list_all(self, *, include_disabled: bool = True) -> list[OneCLIIdentityMapping]:
        mappings = await self.context.onecli_identity_mapping_repo.list(include_deleted=include_disabled)
        if include_disabled:
            return mappings
        return [item for item in mappings if item.status == OneCLIIdentityMappingStatus.ACTIVE]

    async def list_for_owner(self, owner_user_id: str) -> list[OneCLIIdentityMapping]:
        if hasattr(self.context.onecli_identity_mapping_repo, "list_by_owner"):
            return await self.context.onecli_identity_mapping_repo.list_by_owner(owner_user_id)
        mappings = await self.context.onecli_identity_mapping_repo.list()
        return [
            item for item in mappings
            if item.owner_user_id == owner_user_id and item.status == OneCLIIdentityMappingStatus.ACTIVE
        ]

    async def get(self, mapping_id: str) -> OneCLIIdentityMapping | None:
        return await self.context.onecli_identity_mapping_repo.get(mapping_id, include_deleted=True)

    async def get_for_owner(self, mapping_id: str, owner_user_id: str) -> OneCLIIdentityMapping | None:
        item = await self.context.onecli_identity_mapping_repo.get(mapping_id, include_deleted=True)
        if item is None or item.owner_user_id != owner_user_id:
            return None
        return item

    async def _ensure_agent_identity_available(
            self,
            *,
            onecli_agent_id: str,
            owner_user_id: str,
            current_mapping_id: str | None = None,
    ) -> None:
        mappings = await self.context.onecli_identity_mapping_repo.list(include_deleted=True)
        for item in mappings:
            if item.id == current_mapping_id:
                continue
            if item.onecli_agent_id == onecli_agent_id:
                if item.owner_user_id == owner_user_id:
                    raise ValueError("OneCLI agent identity is already mapped for this user.")
                raise ValueError("OneCLI agent identity is already mapped to another Agency user.")

    async def create_for_owner(
            self,
            payload: dict[str, Any],
            owner_user_id: str,
            *,
            actor_user_id: str | None = None,
            admin: bool = False,
    ) -> OneCLIIdentityMapping:
        payload = dict(payload)
        requested_owner = payload.get("owner_user_id", owner_user_id)
        if requested_owner != owner_user_id:
            raise ValueError("OneCLI identity mappings must belong to the current user.")
        payload["owner_user_id"] = owner_user_id
        payload = self._with_default_rule_profile_metadata(payload)
        item = OneCLIIdentityMapping.model_validate(payload)
        await self._ensure_agent_identity_available(
            onecli_agent_id=item.onecli_agent_id,
            owner_user_id=owner_user_id,
        )
        created = await self.context.onecli_identity_mapping_repo.create(item)
        self._audit_mapping("created", created, actor_user_id=actor_user_id or owner_user_id, admin=admin)
        return created

    async def create_for_admin(
            self,
            payload: dict[str, Any],
            *,
            actor_user_id: str,
    ) -> OneCLIIdentityMapping:
        owner_user_id = str(payload.get("owner_user_id") or "").strip()
        if not owner_user_id:
            raise ValueError("owner_user_id is required for admin OneCLI identity mapping creation.")
        item = OneCLIIdentityMapping.model_validate(
            self._with_default_rule_profile_metadata({**payload, "owner_user_id": owner_user_id})
        )
        await self._ensure_agent_identity_available(
            onecli_agent_id=item.onecli_agent_id,
            owner_user_id=owner_user_id,
        )
        created = await self.context.onecli_identity_mapping_repo.create(item)
        self._audit_mapping("created", created, actor_user_id=actor_user_id, admin=True)
        return created

    async def update_for_owner(
            self,
            mapping_id: str,
            owner_user_id: str,
            patch: dict[str, Any],
            *,
            actor_user_id: str | None = None,
            admin: bool = False,
    ) -> OneCLIIdentityMapping | None:
        current = await self.get_for_owner(mapping_id, owner_user_id)
        if current is None:
            return None
        patch = dict(patch)
        if "owner_user_id" in patch and patch["owner_user_id"] != owner_user_id:
            raise ValueError("OneCLI identity mappings cannot be moved across users.")
        patch.pop("owner_user_id", None)
        merged = current.model_dump(mode="json")
        merged.update(patch)
        merged["owner_user_id"] = owner_user_id
        item = OneCLIIdentityMapping.model_validate(merged)
        await self._ensure_agent_identity_available(
            onecli_agent_id=item.onecli_agent_id,
            owner_user_id=owner_user_id,
            current_mapping_id=mapping_id,
        )
        saved = await self.context.onecli_identity_mapping_repo.save(item)
        self._audit_mapping("updated", saved, actor_user_id=actor_user_id or owner_user_id, admin=admin)
        return saved

    async def update_as_admin(
            self,
            mapping_id: str,
            patch: dict[str, Any],
            *,
            actor_user_id: str,
    ) -> OneCLIIdentityMapping | None:
        current = await self.get(mapping_id)
        if current is None:
            return None
        patch = dict(patch)
        if "owner_user_id" in patch and patch["owner_user_id"] != current.owner_user_id:
            raise ValueError("OneCLI identity mappings cannot be moved across users.")
        patch.pop("owner_user_id", None)
        merged = current.model_dump(mode="json")
        merged.update(patch)
        merged["owner_user_id"] = current.owner_user_id
        item = OneCLIIdentityMapping.model_validate(merged)
        await self._ensure_agent_identity_available(
            onecli_agent_id=item.onecli_agent_id,
            owner_user_id=item.owner_user_id,
            current_mapping_id=mapping_id,
        )
        saved = await self.context.onecli_identity_mapping_repo.save(item)
        self._audit_mapping("updated", saved, actor_user_id=actor_user_id, admin=True)
        return saved

    async def disable_for_owner(
            self,
            mapping_id: str,
            owner_user_id: str,
            *,
            actor_user_id: str | None = None,
            admin: bool = False,
    ) -> bool:
        current = await self.get_for_owner(mapping_id, owner_user_id)
        if current is None:
            return False
        updated = current.model_copy(update={"status": OneCLIIdentityMappingStatus.DISABLED})
        saved = await self.context.onecli_identity_mapping_repo.save(updated)
        self._audit_mapping("disabled", saved, actor_user_id=actor_user_id or owner_user_id, admin=admin)
        return True

    async def disable_as_admin(self, mapping_id: str, *, actor_user_id: str) -> bool:
        current = await self.get(mapping_id)
        if current is None:
            return False
        updated = current.model_copy(update={"status": OneCLIIdentityMappingStatus.DISABLED})
        saved = await self.context.onecli_identity_mapping_repo.save(updated)
        self._audit_mapping("disabled", saved, actor_user_id=actor_user_id, admin=True)
        return True

    async def disable_active_for_owner(
            self,
            owner_user_id: str,
            *,
            actor_user_id: str | None = None,
            reason: str,
            credential_id: str | None = None,
            admin: bool = False,
    ) -> list[OneCLIIdentityMapping]:
        mappings = await self.list_for_owner(owner_user_id)
        disabled: list[OneCLIIdentityMapping] = []
        for current in mappings:
            updated = current.model_copy(update={"status": OneCLIIdentityMappingStatus.DISABLED})
            saved = await self.context.onecli_identity_mapping_repo.save(updated)
            self._audit_mapping(
                "disabled",
                saved,
                actor_user_id=actor_user_id,
                admin=admin,
                reason=reason,
                credential_id=credential_id,
            )
            disabled.append(saved)
        return disabled

    async def disable_active_for_workflow(
            self,
            workflow_id: str,
            *,
            actor_user_id: str,
            reason: str,
    ) -> list[OneCLIIdentityMapping]:
        mappings = await self.list_all(include_disabled=False)
        disabled: list[OneCLIIdentityMapping] = []
        for current in mappings:
            if current.workflow_id != workflow_id:
                continue
            updated = current.model_copy(update={"status": OneCLIIdentityMappingStatus.DISABLED})
            saved = await self.context.onecli_identity_mapping_repo.save(updated)
            self._audit_mapping(
                "disabled",
                saved,
                actor_user_id=actor_user_id,
                admin=True,
                reason=reason,
            )
            disabled.append(saved)
        return disabled

    async def resolve_agent_token_secret_ref(
            self,
            *,
            owner_user_id: str | None,
            workflow_id: str | None = None,
    ) -> str | None:
        context = await self.resolve_agent_token_context(owner_user_id=owner_user_id, workflow_id=workflow_id)
        value = context.get("agent_token_secret_ref")
        return value if isinstance(value, str) else None

    async def resolve_agent_token_context(
            self,
            *,
            owner_user_id: str | None,
            workflow_id: str | None = None,
    ) -> dict[str, Any]:
        if owner_user_id:
            mappings = await self.list_for_owner(owner_user_id)
            active = [item for item in mappings if item.status == OneCLIIdentityMappingStatus.ACTIVE]
            if workflow_id:
                for item in active:
                    if item.workflow_id == workflow_id:
                        self._audit_mapping("used", item, actor_user_id=owner_user_id)
                        return {
                            "agent_token_secret_ref": item.agent_token_secret_ref,
                            "source": "workflow_mapping",
                            "mapping_id": item.id,
                            "onecli_agent_id": item.onecli_agent_id,
                            "owner_user_id": item.owner_user_id,
                            "workflow_id": item.workflow_id,
                        }
            for item in active:
                if item.workflow_id is None:
                    self._audit_mapping("used", item, actor_user_id=owner_user_id)
                    return {
                        "agent_token_secret_ref": item.agent_token_secret_ref,
                        "source": "user_mapping",
                        "mapping_id": item.id,
                        "onecli_agent_id": item.onecli_agent_id,
                        "owner_user_id": item.owner_user_id,
                        "workflow_id": item.workflow_id,
                    }
        settings = get_settings()
        if settings.onecli_allow_global_agent_token_fallback and settings.onecli_agent_token_secret_ref:
            self.context.runtime_operations.record_action(
                "onecli.global_agent_token_fallback.used",
                owner_user_id=owner_user_id,
                workflow_id=workflow_id,
                app_env=settings.app_env,
                multi_user_mode=settings.onecli_multi_user_mode,
                agent_token_secret_ref_configured=True,
            )
            return {
                "agent_token_secret_ref": settings.onecli_agent_token_secret_ref,
                "source": "development_global_fallback",
                "owner_user_id": owner_user_id,
                "workflow_id": workflow_id,
            }
        return {
            "agent_token_secret_ref": None,
            "source": "none",
            "owner_user_id": owner_user_id,
            "workflow_id": workflow_id,
        }
