from __future__ import annotations

import asyncio
import hashlib
import hmac
import httpx
import json
import time
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode, urlparse
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.core.config import reset_settings_cache
from app.domain import (
    AgentDefinition,
    ConnectorInstallation,
    CredentialDefinition,
    MCPExposureSettings,
    MemoryRecord,
    ModelProfileDefinition,
    SecuritySettings,
    ToolDefinition,
    ToolImplementationReference,
    ToolType,
    UserDefinition,
    WorkflowDefinition,
)
from app.llm.base import ModelResponse, ModelToolCall
from app.llm.registry import LLMEnvironmentConfig
from app.services.runtime_secrets import seal_runtime_secret
from app.services.conversations.channel_adapters import (
    AdapterInboundMessage,
    create_channel_outbound_formatter,
    create_chat_channel_adapter,
)
from app.services.main_agent_setup.service import MainAgentSetupConfig, MainAgentSetupService
from app.services.persona_factory import PersonaFactoryService


class _FakeModelClient:
    provider_key = "fake"
    responses: list[ModelResponse] = []
    last_system_message: str | None = None

    def __init__(self, profile: ModelProfileDefinition, env: LLMEnvironmentConfig):
        self.profile = profile
        self.env = env

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        _FakeModelClient.last_system_message = next((item.content for item in messages if item.role == "system"), None)
        if _FakeModelClient.responses:
            return _FakeModelClient.responses.pop(0)
        return ModelResponse(content="direct reply", provider="fake", model=self.profile.model)

    def generate_structured(self, messages, *, schema, temperature=None, max_tokens=None, **kwargs):
        schema_name = kwargs.get("schema_name")
        if schema_name == "source_intelligence_classification":
            return ModelResponse(
                content={
                    "label": "decision",
                    "confidence": 0.92,
                    "signals": ["test-fixture"],
                    "document_kind": "unknown",
                    "content_roles": ["decision"],
                    "extraction_targets": ["decision_pattern"],
                    "memory_layers": ["semantic"],
                    "vector_tags": ["test-fixture"],
                    "graph_entities": [],
                    "graph_relationships": [],
                    "should_include": True,
                    "rationale": "Fixture classification for persona source distillation.",
                },
                provider="fake",
                model=self.profile.model,
            )
        if schema_name == "persona_factory_normalization":
            return ModelResponse(
                content={
                    "updates": [],
                    "superseded": [],
                    "conflict_groups": [],
                    "summary": "No normalization changes required for fixture inputs.",
                },
                provider="fake",
                model=self.profile.model,
            )
        if schema_name == "persona_llm_distillation_candidates":
            evidence = "Transport Persona reviews evidence and risk with source-backed judgment."
            return ModelResponse(
                content={
                    "candidates": [
                        {
                            "item_type": "decision_pattern",
                            "memory_layer": "procedural",
                            "title": "Evidence-backed review pattern",
                            "content": "Review observations for evidence quality and risk before responding.",
                            "confidence": 0.88,
                            "source_evidence": evidence,
                            "source_span": {"start": 0, "end": len(evidence)},
                            "review_reasons": [],
                            "structured_payload": {},
                            "inference_type": "extractive",
                            "unsupported_claim_risk": 0.05,
                            "conflict_signals": [],
                            "suggested_graph_entities": [],
                            "suggested_graph_relationships": [],
                            "needs_review": False,
                        }
                    ]
                },
                provider="fake",
                model=self.profile.model,
            )
        return ModelResponse(content={"ok": True}, provider="fake", model=self.profile.model)

    def stream_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        yield "unused"

    def count_tokens(self, messages, **kwargs):
        return 0

    def health_check(self):
        return {"ok": True}


class ConversationChannelsRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = create_test_api_context()
        self.context.llm_provider_registry.register("fake", lambda profile, env: _FakeModelClient(profile, env))
        asyncio.run(
            self.context.model_profile_repo.save(
                ModelProfileDefinition(id="profile-fake", name="Fake", provider="fake", model="fake-model")
            )
        )
        asyncio.run(
            MainAgentSetupService(self.context).create_main_agent(
                MainAgentSetupConfig(
                    agent_name="Main Agent",
                    agent_description="Configured for tests.",
                    agent_instructions="Answer briefly.",
                    model_profile_id="profile-fake",
                    profile_id="main-agent-profile",
                )
            )
        )
        _FakeModelClient.responses = []
        _FakeModelClient.last_system_message = None
        self.client = TestClient(create_app(context=self.context))

    def _trust_channel_user(
        self,
        *,
        user_id: str,
        channel_type: str,
        channel_user_id: str,
        channel_display_name: str | None = None,
    ) -> None:
        asyncio.run(
            self.context.user_repo.save(
                UserDefinition(id=user_id, email=f"{user_id}@example.com", display_name=user_id)
            )
        )
        mapping = self.client.post(
            "/integrations/conversations/channel-identity-mappings",
            headers={"x-agency-user-id": user_id},
            json={
                "channel_type": channel_type,
                "channel_user_id": channel_user_id,
                "internal_user_id": user_id,
                "channel_display_name": channel_display_name,
                "trusted": True,
            },
        )
        self.assertEqual(mapping.status_code, 200)

    def _publish_minimal_persona(
        self,
        *,
        user_id: str,
        persona_name: str,
        memory_id: str,
    ) -> dict:
        asyncio.run(
            self.context.user_repo.save(
                UserDefinition(id=user_id, email=f"{user_id}@example.com", display_name=user_id)
            )
        )
        asyncio.run(
            self.context.memory_repo.create(
                MemoryRecord(
                    id=memory_id,
                    scope="user",
                    created_by_user_id=user_id,
                    content=f"{persona_name} reviews evidence and risk with source-backed judgment.",
                    summary=f"{persona_name} source memory",
                    memory_type="decision",
                    tags=["persona-source"],
                )
            )
        )
        factory = PersonaFactoryService(self.context)
        distill = asyncio.run(
            factory.distill_from_memories(
                persona_id=None,
                name=persona_name,
                description=f"{persona_name} description.",
                source_memory_ids=[memory_id],
                model_profile_id=None,
                current_user=asyncio.run(self.context.user_repo.get(user_id)),
            )
        )
        item_id = distill["items"][0]["id"]
        run_id = distill["run"]["id"]
        asyncio.run(factory.approve_item(item_id))
        asyncio.run(factory.synthesize_package_from_items(run_id))
        approved = asyncio.run(factory.approve_run(run_id, current_user=asyncio.run(self.context.user_repo.get(user_id))))
        return asyncio.run(factory.publish_run(run_id, current_user=asyncio.run(self.context.user_repo.get(user_id))))

    def _create_delivery_user_and_credential(
        self,
        *,
        user_id: str,
        credential_id: str,
        provider: str,
        secret_ref: str,
        metadata: dict | None = None,
    ) -> None:
        asyncio.run(
            self.context.user_repo.save(
                UserDefinition(id=user_id, email=f"{user_id}@example.com", display_name=user_id)
            )
        )
        asyncio.run(
            self.context.credential_repo.create(
                CredentialDefinition(
                    id=credential_id,
                    owner_user_id=user_id,
                    name=f"{provider} delivery",
                    provider=provider,
                    secret_ref=secret_ref,
                    metadata=metadata or {},
                )
            )
        )

    def test_external_channel_can_resolve_and_reuse_conversation(self) -> None:
        first = self.client.post(
            "/integrations/conversations/channels/telegram/messages",
            json={
                "channel_thread_id": "thread-1",
                "channel_user_id": "telegram-user-1",
                "channel_display_name": "Telegram User",
                "text": "Hello from Telegram",
                "external_message_id": "tg-msg-1",
            },
        )
        self.assertEqual(first.status_code, 200)
        payload = first.json()
        self.assertEqual(payload["conversation"]["channel_type"], "telegram")
        self.assertEqual(payload["outbound_messages"][0]["type"], "text")

        second = self.client.post(
            "/integrations/conversations/channels/telegram/resolve",
            json={
                "channel_thread_id": "thread-1",
                "channel_user_id": "telegram-user-1",
                "channel_display_name": "Telegram User",
            },
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["id"], payload["conversation"]["id"])

    def test_duplicate_external_message_id_replays_existing_response(self) -> None:
        first = self.client.post(
            "/integrations/conversations/channels/telegram/messages",
            json={
                "channel_thread_id": "thread-idempotent",
                "channel_user_id": "telegram-user-idempotent",
                "text": "Hello once",
                "external_message_id": "tg-idempotent-1",
            },
        )
        self.assertEqual(first.status_code, 200)
        conversation_id = first.json()["conversation"]["id"]

        second = self.client.post(
            "/integrations/conversations/channels/telegram/messages",
            json={
                "channel_thread_id": "thread-idempotent",
                "channel_user_id": "telegram-user-idempotent",
                "text": "Hello once",
                "external_message_id": "tg-idempotent-1",
            },
        )
        self.assertEqual(second.status_code, 200)
        replay = second.json()
        self.assertTrue(replay["result"]["idempotent"])
        self.assertEqual(replay["outbound_messages"][0]["text"], first.json()["outbound_messages"][0]["text"])

        messages = asyncio.run(self.context.conversation_message_repo.list_by_conversation(conversation_id))
        self.assertEqual(len(messages), 2)

    def test_telegram_adapter_webhook_handles_message_and_duplicate_replay(self) -> None:
        token = "telegram-replay-webhook-secret"
        self._create_delivery_user_and_credential(
            user_id="user-telegram-replay",
            credential_id="credential-telegram-replay",
            provider="telegram",
            secret_ref="env://TELEGRAM_BOT_TOKEN",
            metadata={"webhook_secret_token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest()},
        )
        payload = {
            "update_id": 1001,
            "message": {
                "message_id": 2002,
                "chat": {"id": 3003, "type": "private"},
                "from": {"id": 4004, "username": "telegram_user"},
                "text": "Hello through Telegram adapter",
            },
        }
        first = self.client.post(
            "/integrations/conversations/adapters/telegram/webhook",
            params={"credential_id": "credential-telegram-replay"},
            headers={"x-telegram-bot-api-secret-token": token},
            json=payload,
        )
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertTrue(body["handled"])
        self.assertEqual(body["adapter"], "telegram")
        self.assertEqual(body["event_type"], "message")
        self.assertEqual(body["conversation"]["channel_user_id"], "4004")
        self.assertEqual(body["outbound_messages"][0]["type"], "text")
        self.assertEqual(body["provider_outbound_messages"][0]["method"], "sendMessage")
        self.assertEqual(body["provider_outbound_messages"][0]["payload"]["chat_id"], "3003")

        second = self.client.post(
            "/integrations/conversations/adapters/telegram/webhook",
            params={"credential_id": "credential-telegram-replay"},
            headers={"x-telegram-bot-api-secret-token": token},
            json=payload,
        )
        self.assertEqual(second.status_code, 200)
        replay = second.json()
        self.assertTrue(replay["result"]["idempotent"])
        self.assertEqual(replay["outbound_messages"][0]["text"], body["outbound_messages"][0]["text"])
        self.assertEqual(replay["provider_outbound_messages"][0]["payload"]["text"], "direct reply")

    def test_telegram_adapter_webhook_verifies_secret_token_when_credential_supplied(self) -> None:
        self._create_delivery_user_and_credential(
            user_id="user-telegram-webhook",
            credential_id="credential-telegram-webhook",
            provider="telegram",
            secret_ref="env://TELEGRAM_BOT_TOKEN",
            metadata={"webhook_secret_ref": "env://TELEGRAM_WEBHOOK_SECRET"},
        )
        payload = {
            "update_id": 1101,
            "message": {
                "message_id": 2102,
                "chat": {"id": 3103, "type": "private"},
                "from": {"id": 4104, "username": "telegram_webhook"},
                "text": "Verified Telegram webhook",
            },
        }

        with patch.dict(
            "os.environ",
            {"TELEGRAM_WEBHOOK_SECRET": "telegram-webhook-secret"},
            clear=False,
        ):
            rejected = self.client.post(
                "/integrations/conversations/adapters/telegram/webhook",
                params={"credential_id": "credential-telegram-webhook"},
                headers={"x-telegram-bot-api-secret-token": "wrong"},
                json=payload,
            )
            accepted = self.client.post(
                "/integrations/conversations/adapters/telegram/webhook",
                params={"credential_id": "credential-telegram-webhook"},
                headers={"x-telegram-bot-api-secret-token": "telegram-webhook-secret"},
                json=payload,
            )

        self.assertEqual(rejected.status_code, 422)
        self.assertIn("Telegram webhook secret token verification failed", rejected.json()["detail"])
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(accepted.json()["webhook_verification"]["verified"])

    def test_telegram_adapter_webhook_verifies_generated_token_hash(self) -> None:
        token = "generated-telegram-webhook-secret"
        self._create_delivery_user_and_credential(
            user_id="user-telegram-webhook-hash",
            credential_id="credential-telegram-webhook-hash",
            provider="telegram",
            secret_ref="env://TELEGRAM_BOT_TOKEN",
            metadata={
                "webhook_secret_token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            },
        )
        payload = {
            "update_id": 1102,
            "message": {
                "message_id": 2103,
                "chat": {"id": 3104, "type": "private"},
                "from": {"id": 4105, "username": "telegram_webhook_hash"},
                "text": "Verified Telegram webhook hash",
            },
        }

        rejected = self.client.post(
            "/integrations/conversations/adapters/telegram/webhook",
            params={"credential_id": "credential-telegram-webhook-hash"},
            headers={"x-telegram-bot-api-secret-token": "wrong"},
            json=payload,
        )
        accepted = self.client.post(
            "/integrations/conversations/adapters/telegram/webhook",
            params={"credential_id": "credential-telegram-webhook-hash"},
            headers={"x-telegram-bot-api-secret-token": token},
            json=payload,
        )

        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(accepted.json()["webhook_verification"]["verified"])

    def test_telegram_adapter_webhook_delivers_outbound_messages_when_credential_supplied(self) -> None:
        token = "telegram-delivery-webhook-secret"
        self._create_delivery_user_and_credential(
            user_id="user-telegram-webhook-delivery",
            credential_id="credential-telegram-webhook-delivery",
            provider="telegram",
            secret_ref="env://TELEGRAM_BOT_TOKEN",
            metadata={"webhook_secret_token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest()},
        )
        payload = {
            "update_id": 1201,
            "message": {
                "message_id": 2202,
                "chat": {"id": 3203, "type": "private"},
                "from": {"id": 4204, "username": "telegram_delivery"},
                "text": "Deliver Telegram reply",
            },
        }

        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "telegram-token"}, clear=False), patch(
            "app.api.routes.conversations.channels.ChannelOutboundDeliveryService.deliver_for_owner",
            new=AsyncMock(return_value={"ok": True, "deliveries": []}),
        ) as delivery_mock:
            response = self.client.post(
                "/integrations/conversations/adapters/telegram/webhook",
                params={"credential_id": "credential-telegram-webhook-delivery"},
                headers={"x-telegram-bot-api-secret-token": token},
                json=payload,
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["handled"])
        self.assertIn("delivery", body)
        delivery_mock.assert_awaited_once()

    def test_telegram_adapter_webhook_treats_delivery_failures_as_best_effort(self) -> None:
        token = "telegram-delivery-failure-webhook-secret"
        self._create_delivery_user_and_credential(
            user_id="user-telegram-webhook-delivery-failure",
            credential_id="credential-telegram-webhook-delivery-failure",
            provider="telegram",
            secret_ref="env://TELEGRAM_BOT_TOKEN",
            metadata={"webhook_secret_token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest()},
        )
        payload = {
            "update_id": 1202,
            "message": {
                "message_id": 2203,
                "chat": {"id": 3204, "type": "private"},
                "from": {"id": 4205, "username": "telegram_delivery_failure"},
                "text": "Telegram delivery should not fail the webhook",
            },
        }

        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "telegram-token"}, clear=False), patch(
            "app.api.routes.conversations.channels.ChannelOutboundDeliveryService.deliver_for_owner",
            new=AsyncMock(side_effect=RuntimeError("temporary network failure")),
        ):
            response = self.client.post(
                "/integrations/conversations/adapters/telegram/webhook",
                params={"credential_id": "credential-telegram-webhook-delivery-failure"},
                headers={"x-telegram-bot-api-secret-token": token},
                json=payload,
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["handled"])
        self.assertEqual(body["delivery"]["ok"], False)
        self.assertTrue(body["delivery"]["skipped"])
        self.assertIn("Outbound delivery failed", body["delivery"]["reason"])

    def test_adapter_webhook_requires_connector_credential_in_test_environment(self) -> None:
        payload = {
            "update_id": 1201,
            "message": {
                "message_id": 2202,
                "chat": {"id": 3203, "type": "private"},
                "from": {"id": 4204, "username": "telegram_production"},
                "text": "Unsigned production webhook",
            },
        }

        response = self.client.post("/integrations/conversations/adapters/telegram/webhook", json=payload)

        self.assertEqual(response.status_code, 422)
        self.assertIn("connector credential is required", response.json()["detail"])

    def test_telegram_webhook_requires_verification_secret_in_test_environment(self) -> None:
        self._create_delivery_user_and_credential(
            user_id="user-telegram-missing-verification",
            credential_id="credential-telegram-missing-verification",
            provider="telegram",
            secret_ref="env://TELEGRAM_BOT_TOKEN",
        )
        response = self.client.post(
            "/integrations/conversations/adapters/telegram/webhook",
            params={"credential_id": "credential-telegram-missing-verification"},
            headers={"x-telegram-bot-api-secret-token": "attacker-selected"},
            json={
                "update_id": 1203,
                "message": {
                    "message_id": 2204,
                    "chat": {"id": 3205, "type": "private"},
                    "from": {"id": 4206, "username": "telegram_missing_verification"},
                    "text": "Credential without a verification secret",
                },
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("requires webhook_secret_ref", response.json()["detail"])

    def test_telegram_adapter_webhook_handles_approval_callback(self) -> None:
        token = "telegram-approval-webhook-secret"
        self._create_delivery_user_and_credential(
            user_id="user-telegram-approval",
            credential_id="credential-telegram-approval",
            provider="telegram",
            secret_ref="env://TELEGRAM_BOT_TOKEN",
            metadata={"webhook_secret_token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest()},
        )
        response = self.client.post(
            "/integrations/conversations/channels/telegram/messages",
            json={
                "channel_thread_id": "thread-adapter-approval",
                "channel_user_id": "telegram-approval-user",
                "text": "Please request approval",
                "content": {
                    "approval_request": {
                        "approval_type": "workflow_execution",
                        "target_type": "workflow",
                        "target_id": "workflow-approval-adapter",
                        "summary": "Run protected workflow workflow-approval-adapter",
                    }
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        approval_id = response.json()["result"]["approval_request"]["id"]

        callback_payload = {
            "update_id": 1002,
            "callback_query": {
                "id": "callback-1",
                "from": {"id": "telegram-approval-user", "username": "approver"},
                "data": f"approval:approve:{approval_id}",
            },
        }
        unsigned = self.client.post(
            "/integrations/conversations/adapters/telegram/webhook",
            params={"credential_id": "credential-telegram-approval"},
            json=callback_payload,
        )
        callback = self.client.post(
            "/integrations/conversations/adapters/telegram/webhook",
            params={"credential_id": "credential-telegram-approval"},
            headers={"x-telegram-bot-api-secret-token": token},
            json=callback_payload,
        )
        self.assertEqual(unsigned.status_code, 422)
        self.assertIn("secret token verification failed", unsigned.json()["detail"])
        self.assertEqual(callback.status_code, 200)
        body = callback.json()
        self.assertTrue(body["handled"])
        self.assertEqual(body["event_type"], "approval_action")
        self.assertEqual(body["outbound_messages"][0]["type"], "text")
        self.assertIn("Approval granted", body["outbound_messages"][0]["text"])
        self.assertEqual(body["provider_outbound_messages"][0]["method"], "sendMessage")

    def test_discord_adapter_webhook_uses_trusted_identity_mapping(self) -> None:
        asyncio.run(
            self.context.user_repo.save(
                UserDefinition(id="user-discord", email="discord@example.com", display_name="Discord User")
            )
        )
        mapping = self.client.post(
            "/integrations/conversations/channel-identity-mappings",
            headers={"x-agency-user-id": "user-discord"},
            json={
                "channel_type": "discord",
                "channel_user_id": "discord-user-1",
                "internal_user_id": "user-discord",
                "trusted": True,
            },
        )
        self.assertEqual(mapping.status_code, 200)

        with patch(
            "app.services.conversations.channel_webhooks.ChannelWebhookVerificationService.verify",
            new=AsyncMock(return_value={"verified": True, "required": True, "provider": "discord"}),
        ):
            response = self.client.post(
                "/integrations/conversations/adapters/discord/webhook",
                json={
                    "id": "discord-message-1",
                    "channel_id": "discord-channel-1",
                    "guild_id": "discord-guild-1",
                    "author": {"id": "discord-user-1", "username": "discord_user"},
                    "content": "Hello through Discord adapter",
                },
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["handled"])
        self.assertEqual(body["conversation"]["channel_type"], "discord")
        self.assertEqual(body["conversation"]["created_by_user_id"], "user-discord")
        self.assertEqual(body["provider_outbound_messages"][0]["method"], "createMessage")
        self.assertEqual(body["provider_outbound_messages"][0]["payload"]["channel_id"], "discord-channel-1")

    def test_discord_adapter_webhook_verifies_ed25519_signature_when_credential_supplied(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ).hex()
        self._create_delivery_user_and_credential(
            user_id="user-discord-webhook",
            credential_id="credential-discord-webhook",
            provider="discord",
            secret_ref="env://DISCORD_BOT_TOKEN",
            metadata={"webhook_public_key": public_key},
        )
        payload = {
            "id": "discord-message-signed",
            "channel_id": "discord-channel-signed",
            "author": {"id": "discord-user-signed", "username": "signed_user"},
            "content": "Verified Discord webhook",
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        timestamp = "1777777777"
        signature = private_key.sign(timestamp.encode("utf-8") + body).hex()

        rejected = self.client.post(
            "/integrations/conversations/adapters/discord/webhook",
            params={"credential_id": "credential-discord-webhook"},
            headers={
                "content-type": "application/json",
                "x-signature-ed25519": "00" * 64,
                "x-signature-timestamp": timestamp,
            },
            content=body,
        )
        accepted = self.client.post(
            "/integrations/conversations/adapters/discord/webhook",
            params={"credential_id": "credential-discord-webhook"},
            headers={
                "content-type": "application/json",
                "x-signature-ed25519": signature,
                "x-signature-timestamp": timestamp,
            },
            content=body,
        )

        self.assertEqual(rejected.status_code, 422)
        self.assertIn("Discord webhook signature verification failed", rejected.json()["detail"])
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(accepted.json()["webhook_verification"]["verified"])

    def test_whatsapp_adapter_webhook_ignores_untrusted_identity_mapping(self) -> None:
        asyncio.run(
            self.context.user_repo.save(
                UserDefinition(id="user-whatsapp", email="whatsapp@example.com", display_name="WhatsApp User")
            )
        )
        mapping = self.client.post(
            "/integrations/conversations/channel-identity-mappings",
            headers={"x-agency-user-id": "user-whatsapp"},
            json={
                "channel_type": "whatsapp",
                "channel_user_id": "15551234567",
                "internal_user_id": "user-whatsapp",
                "trusted": False,
            },
        )
        self.assertEqual(mapping.status_code, 200)

        with patch(
            "app.services.conversations.channel_webhooks.ChannelWebhookVerificationService.verify",
            new=AsyncMock(return_value={"verified": True, "required": True, "provider": "whatsapp"}),
        ):
            response = self.client.post(
                "/integrations/conversations/adapters/whatsapp/webhook",
                json={
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "metadata": {"phone_number_id": "phone-number-1"},
                                    "contacts": [{"wa_id": "15551234567", "profile": {"name": "WhatsApp User"}}],
                                    "messages": [
                                        {
                                            "id": "wamid.1",
                                            "from": "15551234567",
                                            "text": {"body": "Hello through WhatsApp adapter"},
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                ]
                },
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["handled"])
        self.assertEqual(body["conversation"]["channel_type"], "whatsapp")
        self.assertIsNone(body["conversation"]["created_by_user_id"])
        self.assertEqual(body["provider_outbound_messages"][0]["method"], "messages")
        self.assertEqual(body["provider_outbound_messages"][0]["payload"]["to"], "15551234567")

    def test_whatsapp_adapter_webhook_verifies_hmac_signature_when_credential_supplied(self) -> None:
        self._create_delivery_user_and_credential(
            user_id="user-whatsapp-webhook",
            credential_id="credential-whatsapp-webhook",
            provider="whatsapp",
            secret_ref="env://WHATSAPP_TOKEN",
            metadata={
                "phone_number_id": "phone-number-signed",
                "app_secret_ref": "env://WHATSAPP_APP_SECRET",
            },
        )
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "phone-number-signed"},
                                "messages": [
                                    {
                                        "id": "wamid.signed",
                                        "from": "15550002222",
                                        "text": {"body": "Verified WhatsApp webhook"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        signature = "sha256=" + hmac.new(b"whatsapp-app-secret", body, hashlib.sha256).hexdigest()

        with patch.dict("os.environ", {"WHATSAPP_APP_SECRET": "whatsapp-app-secret"}, clear=False):
            rejected = self.client.post(
                "/integrations/conversations/adapters/whatsapp/webhook",
                params={"credential_id": "credential-whatsapp-webhook"},
                headers={"content-type": "application/json", "x-hub-signature-256": "sha256=bad"},
                content=body,
            )
            accepted = self.client.post(
                "/integrations/conversations/adapters/whatsapp/webhook",
                params={"credential_id": "credential-whatsapp-webhook"},
                headers={"content-type": "application/json", "x-hub-signature-256": signature},
                content=body,
            )

        self.assertEqual(rejected.status_code, 422)
        self.assertIn("WhatsApp webhook signature verification failed", rejected.json()["detail"])
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(accepted.json()["webhook_verification"]["verified"])

    def test_provider_outbound_formatters_render_approval_actions(self) -> None:
        target = AdapterInboundMessage(
            channel_type="telegram",
            channel_thread_id="thread-provider-format",
            channel_user_id="user-provider-format",
            channel_display_name=None,
            text="Approve this",
            external_message_id=None,
            metadata={},
        )
        approval = {
            "type": "approval",
            "text": "Approve workflow?",
            "approval_request_id": "approval-provider-format",
            "actions": [{"type": "approve"}, {"type": "reject"}],
        }

        telegram = create_channel_outbound_formatter("telegram").format_messages([approval], target=target)[0]
        self.assertEqual(telegram["payload"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                         "approval:approve:approval-provider-format")

        discord = create_channel_outbound_formatter("discord").format_messages([approval], target=target)[0]
        self.assertEqual(discord["payload"]["components"][0]["components"][0]["custom_id"],
                         "approval:approve:approval-provider-format")

        whatsapp = create_channel_outbound_formatter("whatsapp").format_messages([approval], target=target)[0]
        self.assertEqual(whatsapp["payload"]["interactive"]["action"]["buttons"][0]["reply"]["id"],
                         "approval:approve:approval-provider-format")

    def test_provider_outbound_formatters_render_structured_payloads_safely(self) -> None:
        target = AdapterInboundMessage(
            channel_type="discord",
            channel_thread_id="thread-structured-format",
            channel_user_id="user-structured-format",
            channel_display_name=None,
            text="",
            external_message_id=None,
            metadata={},
        )
        structured_message = {
            "type": "event",
            "content": {
                "message_type": "workflow_update",
                "status": "completed",
                "summary": "Workflow updated successfully",
            },
        }

        telegram = create_channel_outbound_formatter("telegram").format_messages([structured_message], target=target)[0]
        discord = create_channel_outbound_formatter("discord").format_messages([structured_message], target=target)[0]
        whatsapp = create_channel_outbound_formatter("whatsapp").format_messages([structured_message], target=target)[0]

        self.assertTrue(telegram["payload"]["text"])
        self.assertIn("Workflow updated successfully", discord["payload"]["content"])
        self.assertIn("Workflow updated successfully", whatsapp["payload"]["text"]["body"])

    def test_slack_adapter_parses_messages_commands_and_approval_callbacks(self) -> None:
        adapter = create_chat_channel_adapter(self.context, "slack")
        message = adapter.parse_message(
            {
                "type": "event_callback",
                "team_id": "T-slack",
                "event_id": "Ev-slack-1",
                "event": {
                    "type": "app_mention",
                    "channel": "C-slack-1",
                    "user": "U-slack-1",
                    "text": "<@U-app> can you help?",
                    "ts": "1712345678.000100",
                },
            }
        )
        self.assertIsNotNone(message)
        self.assertEqual(message.channel_type, "slack")
        self.assertEqual(message.channel_thread_id, "C-slack-1")
        self.assertEqual(message.text, "<@U-app> can you help?")
        self.assertEqual(message.metadata["channel_id"], "C-slack-1")

        command = adapter.parse_message(
            {
                "command": "/agent",
                "text": "inspect current workflow",
                "channel_id": "C-slack-1",
                "user_id": "U-slack-1",
                "user_name": "Slack User",
                "team_id": "T-slack",
                "trigger_id": "1337.1",
            }
        )
        self.assertIsNotNone(command)
        self.assertEqual(command.text, "/agent inspect current workflow")
        self.assertEqual(command.metadata["command"], "/agent")

        approval = adapter.parse_approval_action(
            {
                "type": "block_actions",
                "team": {"id": "T-slack"},
                "user": {"id": "U-slack-1", "username": "slack_user"},
                "container": {"type": "message", "channel_id": "C-slack-1", "message_ts": "1712345678.000100"},
                "actions": [
                    {
                        "action_id": "approval_approve",
                        "value": "approval:approve:approval-slack-1",
                    }
                ],
            }
        )
        self.assertIsNotNone(approval)
        self.assertEqual(approval.approval_request_id, "approval-slack-1")
        self.assertEqual(approval.metadata["thread_ts"], "1712345678.000100")

    def test_slack_adapter_webhook_handles_message_event_and_verifies_signature(self) -> None:
        self._create_delivery_user_and_credential(
            user_id="user-slack-webhook",
            credential_id="credential-slack-webhook",
            provider="slack",
            secret_ref="env://SLACK_BOT_TOKEN",
            metadata={"workspace_id": "T-slack", "signing_secret_ref": "env://SLACK_SIGNING_SECRET"},
        )
        payload = {
            "type": "event_callback",
            "team_id": "T-slack",
            "event_id": "Ev-slack-2",
            "event": {
                "type": "message",
                "channel": "C-slack-2",
                "user": "U-slack-2",
                "text": "Hello from Slack",
                "ts": "1712345678.000200",
            },
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        timestamp = str(int(time.time()))
        signature = "v0=" + hmac.new(
            b"slack-signing-secret",
            f"v0:{timestamp}:".encode("utf-8") + body,
            hashlib.sha256,
        ).hexdigest()

        with patch.dict("os.environ", {"SLACK_SIGNING_SECRET": "slack-signing-secret"}, clear=False):
            rejected = self.client.post(
                "/integrations/conversations/adapters/slack/webhook",
                params={"credential_id": "credential-slack-webhook"},
                headers={
                    "content-type": "application/json",
                    "x-slack-signature": "v0=bad",
                    "x-slack-request-timestamp": timestamp,
                },
                content=body,
            )
            accepted = self.client.post(
                "/integrations/conversations/adapters/slack/webhook",
                params={"credential_id": "credential-slack-webhook"},
                headers={
                    "content-type": "application/json",
                    "x-slack-signature": signature,
                    "x-slack-request-timestamp": timestamp,
                },
                content=body,
            )

        self.assertEqual(rejected.status_code, 422)
        self.assertIn("Slack webhook signature verification failed", rejected.json()["detail"])
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(accepted.json()["handled"])
        self.assertEqual(accepted.json()["provider_outbound_messages"][0]["method"], "chat.postMessage")
        self.assertEqual(accepted.json()["provider_outbound_messages"][0]["payload"]["channel"], "C-slack-2")

    def test_slack_webhook_rejects_credential_from_another_workspace(self) -> None:
        self._create_delivery_user_and_credential(
            user_id="user-slack-workspace-a",
            credential_id="credential-slack-workspace-a",
            provider="slack",
            secret_ref="env://SLACK_BOT_TOKEN",
            metadata={"workspace_id": "T-workspace-a", "signing_secret_ref": "env://SLACK_SIGNING_SECRET"},
        )
        payload = {
            "type": "event_callback",
            "team_id": "T-workspace-b",
            "event_id": "Ev-cross-workspace",
            "event": {
                "type": "message",
                "channel": "C-cross-workspace",
                "user": "U-cross-workspace",
                "text": "must not cross installation boundary",
                "ts": "1712345678.000300",
            },
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        timestamp = str(int(time.time()))
        signature = "v0=" + hmac.new(
            b"slack-signing-secret",
            f"v0:{timestamp}:".encode("utf-8") + body,
            hashlib.sha256,
        ).hexdigest()

        with patch.dict("os.environ", {"SLACK_SIGNING_SECRET": "slack-signing-secret"}, clear=False):
            response = self.client.post(
                "/integrations/conversations/adapters/slack/webhook",
                params={"credential_id": "credential-slack-workspace-a"},
                headers={
                    "content-type": "application/json",
                    "x-slack-signature": signature,
                    "x-slack-request-timestamp": timestamp,
                },
                content=body,
            )

        self.assertEqual(response.status_code, 422)
        self.assertIn("team does not match", response.json()["detail"])

    def test_slack_adapter_webhook_handles_block_action_approval_callback(self) -> None:
        self._create_delivery_user_and_credential(
            user_id="user-slack-approval",
            credential_id="credential-slack-approval",
            provider="slack",
            secret_ref="env://SLACK_BOT_TOKEN",
            metadata={"workspace_id": "T-slack", "signing_secret_ref": "env://SLACK_SIGNING_SECRET"},
        )
        response = self.client.post(
            "/integrations/conversations/channels/slack/messages",
            json={
                "channel_thread_id": "C-slack-approval",
                "channel_user_id": "U-slack-approval",
                "text": "Please request approval",
                "content": {
                    "approval_request": {
                        "approval_type": "workflow_execution",
                        "target_type": "workflow",
                        "target_id": "workflow-slack-approval",
                        "summary": "Run protected workflow workflow-slack-approval",
                    }
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        approval_id = response.json()["result"]["approval_request"]["id"]

        callback_payload = {
            "type": "block_actions",
            "team": {"id": "T-slack"},
            "user": {"id": "U-slack-approval", "username": "slack_user"},
            "container": {"type": "message", "channel_id": "C-slack-approval", "message_ts": "1712345678.000300"},
            "actions": [
                {
                    "action_id": "approval_approve",
                    "value": f"approval:approve:{approval_id}",
                }
            ],
        }
        body = urlencode({"payload": json.dumps(callback_payload, separators=(",", ":"))}).encode("utf-8")
        timestamp = str(int(time.time()))
        signature = "v0=" + hmac.new(
            b"slack-signing-secret",
            f"v0:{timestamp}:".encode("utf-8") + body,
            hashlib.sha256,
        ).hexdigest()

        with patch.dict("os.environ", {"SLACK_SIGNING_SECRET": "slack-signing-secret"}, clear=False):
            callback = self.client.post(
                "/integrations/conversations/adapters/slack/webhook",
                params={"credential_id": "credential-slack-approval"},
                headers={
                    "content-type": "application/x-www-form-urlencoded",
                    "x-slack-signature": signature,
                    "x-slack-request-timestamp": timestamp,
                },
                content=body,
            )

        self.assertEqual(callback.status_code, 200)
        body = callback.json()
        self.assertTrue(body["handled"])
        self.assertEqual(body["event_type"], "approval_action")
        self.assertEqual(body["provider_outbound_messages"][0]["method"], "chat.postMessage")

    def test_teams_adapter_parses_message_events_and_formats_outbound_messages(self) -> None:
        adapter = create_chat_channel_adapter(self.context, "microsoft-teams")
        message = adapter.parse_message(
            {
                "type": "message",
                "id": "teams-activity-1",
                "from": {"id": "teams-user-1", "name": "Teams User"},
                "conversation": {"id": "teams-channel-1"},
                "text": "Hello from Teams",
                "channelData": {"team": {"id": "teams-team-1"}, "channel": {"id": "teams-channel-1"}},
            }
        )
        self.assertIsNotNone(message)
        self.assertEqual(message.channel_type, "microsoft-teams")
        self.assertEqual(message.channel_thread_id, "teams-channel-1")
        self.assertEqual(message.metadata["team_id"], "teams-team-1")

        formatter = create_channel_outbound_formatter("teams")
        outbound = formatter.format_messages(
            [{"type": "approval", "approval_request_id": "approval-teams-1", "text": "Approve the run"}],
            target=message,
        )[0]
        self.assertEqual(outbound["method"], "sendChannelMessage")
        self.assertEqual(outbound["payload"]["channel_id"], "teams-channel-1")
        self.assertEqual(outbound["payload"]["content_type"], "html")
        self.assertIn("approval:approve:approval-teams-1", outbound["payload"]["content"])
        self.assertEqual(outbound["payload"]["attachments"][0]["contentType"], "application/vnd.microsoft.card.adaptive")

    def test_teams_adapter_webhook_verifies_signature_when_credential_supplied(self) -> None:
        self._create_delivery_user_and_credential(
            user_id="user-teams-webhook",
            credential_id="credential-teams-webhook",
            provider="microsoft-teams",
            secret_ref="env://TEAMS_BOT_TOKEN",
            metadata={
                "team_id": "teams-team-1",
                "channel_id": "teams-channel-1",
                "webhook_secret_ref": "env://TEAMS_WEBHOOK_SECRET",
            },
        )
        payload = {
            "type": "message",
            "id": "teams-activity-2",
            "from": {"id": "teams-user-2", "name": "Teams User"},
            "conversation": {"id": "teams-channel-2"},
            "text": "Hello through Teams adapter",
            "channelData": {"team": {"id": "teams-team-2"}, "channel": {"id": "teams-channel-2"}},
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(b"teams-webhook-secret", body, hashlib.sha256).hexdigest()

        with patch.dict("os.environ", {"TEAMS_WEBHOOK_SECRET": "teams-webhook-secret"}, clear=False):
            rejected = self.client.post(
                "/integrations/conversations/adapters/microsoft-teams/webhook",
                params={"credential_id": "credential-teams-webhook"},
                headers={"content-type": "application/json", "x-ms-signature": "bad"},
                content=body,
            )
            accepted = self.client.post(
                "/integrations/conversations/adapters/microsoft-teams/webhook",
                params={"credential_id": "credential-teams-webhook"},
                headers={"content-type": "application/json", "x-ms-signature": signature},
                content=body,
            )

        self.assertEqual(rejected.status_code, 422)
        self.assertIn("Microsoft Teams webhook signature verification failed", rejected.json()["detail"])
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(accepted.json()["handled"])
        self.assertEqual(accepted.json()["provider_outbound_messages"][0]["method"], "sendChannelMessage")

    def test_teams_adapter_parses_nested_approval_action_payloads(self) -> None:
        adapter = create_chat_channel_adapter(self.context, "microsoft-teams")
        approval = adapter.parse_approval_action(
            {
                "type": "invoke",
                "from": {"id": "teams-user-approve", "name": "Teams Approver"},
                "conversation": {"id": "teams-channel-approval"},
                "value": {
                    "approval": {
                        "approval_action": "approve",
                        "approval_request_id": "approval-teams-99",
                    }
                },
            }
        )
        self.assertIsNotNone(approval)
        self.assertEqual(approval.approval_request_id, "approval-teams-99")
        self.assertEqual(approval.action, "approve")

    def test_conversation_bound_delivery_sends_slack_message_to_saved_channel(self) -> None:
        self._create_delivery_user_and_credential(
            user_id="user-slack-conversation-delivery",
            credential_id="credential-slack-conversation-delivery",
            provider="slack",
            secret_ref="env://SLACK_BOT_TOKEN",
            metadata={"workspace_id": "workspace-slack"},
        )
        conversation_response = self.client.post(
            "/integrations/conversations/channels/slack/resolve",
            json={
                "channel_thread_id": "C-slack-bound",
                "channel_user_id": "U-slack-bound",
                "channel_display_name": "Slack User",
                "metadata": {"thread_ts": "1712345678.000400"},
            },
        )
        self.assertEqual(conversation_response.status_code, 200)
        conversation_id = conversation_response.json()["id"]

        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "slack-token"}, clear=False), patch(
            "app.services.conversations.channel_delivery.httpx.request",
            return_value=httpx.Response(200, json={"ok": True, "ts": "1712345678.000500"}),
        ) as request_mock:
            response = self.client.post(
                f"/integrations/conversations/channels/{conversation_id}/deliver",
                headers={"x-agency-user-id": "user-slack-conversation-delivery"},
                json={
                    "credential_id": "credential-slack-conversation-delivery",
                    "outbound_messages": [{"type": "text", "text": "Bound Slack delivery"}],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        request_kwargs = request_mock.call_args.kwargs
        self.assertEqual(request_kwargs["url"], "https://slack.com/api/chat.postMessage")
        self.assertEqual(request_kwargs["headers"], {"Authorization": "Bearer slack-token"})
        self.assertEqual(request_kwargs["json"]["channel"], "C-slack-bound")

    def test_discord_adapter_parses_slash_commands_and_component_callbacks(self) -> None:
        adapter = create_chat_channel_adapter(self.context, "discord")
        message = adapter.parse_message(
            {
                "id": "discord-command-1",
                "type": 2,
                "channel_id": "discord-thread-command",
                "guild_id": "discord-guild-command",
                "member": {"user": {"id": "discord-user-command", "username": "cmd_user"}},
                "data": {
                    "name": "summarize",
                    "options": [{"name": "topic", "value": "billing"}],
                },
                "message_reference": {
                    "message_id": "discord-parent-message",
                    "channel_id": "discord-thread-command",
                },
            }
        )

        self.assertIsNotNone(message)
        self.assertEqual(message.text, "/summarize topic=billing")
        self.assertEqual(message.channel_thread_id, "discord-thread-command")
        self.assertEqual(message.metadata["interaction_name"], "summarize")
        self.assertEqual(message.metadata["reply_to_message_id"], "discord-parent-message")

        callback = adapter.parse_message(
            {
                "id": "discord-button-1",
                "type": 3,
                "channel_id": "discord-thread-command",
                "user": {"id": "discord-user-command", "username": "cmd_user"},
                "data": {"custom_id": "refresh_board", "component_type": 2},
            }
        )

        self.assertIsNotNone(callback)
        self.assertEqual(callback.text, "refresh_board")
        self.assertEqual(callback.metadata["interaction_custom_id"], "refresh_board")

    def test_telegram_adapter_parses_replies_commands_and_callback_query_variants(self) -> None:
        adapter = create_chat_channel_adapter(self.context, "telegram")
        message = adapter.parse_message(
            {
                "update_id": 5001,
                "message": {
                    "message_id": 6002,
                    "chat": {"id": 7003, "type": "group"},
                    "from": {"id": 8004, "username": "telegram_user"},
                    "text": "/audit run-now",
                    "entities": [{"offset": 0, "length": 6, "type": "bot_command"}],
                    "reply_to_message": {
                        "message_id": 6001,
                        "from": {"id": 8005, "username": "parent_user"},
                        "text": "Original thread message",
                    },
                },
            }
        )

        self.assertIsNotNone(message)
        self.assertEqual(message.channel_thread_id, "7003")
        self.assertEqual(message.metadata["command_name"], "audit")
        self.assertEqual(message.metadata["command_arguments"], "run-now")
        self.assertEqual(message.metadata["reply_to_message_id"], 6001)

        callback = adapter.parse_message(
            {
                "update_id": 5002,
                "callback_query": {
                    "id": "callback-query-1",
                    "from": {"id": 8004, "username": "telegram_user"},
                    "inline_message_id": "inline-message-1",
                    "data": "refresh_dashboard",
                },
            }
        )

        self.assertIsNotNone(callback)
        self.assertEqual(callback.channel_thread_id, "inline-message-1")
        self.assertEqual(callback.text, "refresh_dashboard")
        self.assertEqual(callback.metadata["callback_query"]["id"], "callback-query-1")

    def test_whatsapp_adapter_parses_non_text_messages_and_status_callbacks(self) -> None:
        adapter = create_chat_channel_adapter(self.context, "whatsapp")
        message = adapter.parse_message(
            {
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "metadata": {"phone_number_id": "phone-number-edge"},
                                    "contacts": [
                                        {"wa_id": "15550003333", "profile": {"name": "WhatsApp User"}}
                                    ],
                                    "messages": [
                                        {
                                            "id": "wamid.image",
                                            "from": "15550003333",
                                            "image": {"id": "media-1"},
                                            "caption": "Photo for review",
                                            "context": {"id": "wamid.parent"},
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                ]
            }
        )

        self.assertIsNotNone(message)
        self.assertEqual(message.text, "Photo for review")
        self.assertEqual(message.metadata["message_type"], "image")
        self.assertEqual(message.metadata["reply_to_message_id"], "wamid.parent")

        status = asyncio.run(
            adapter.handle_webhook(
                {
                    "entry": [
                        {
                            "changes": [
                                {
                                    "value": {
                                        "metadata": {"phone_number_id": "phone-number-edge"},
                                        "statuses": [
                                            {
                                                "id": "wamid.image",
                                                "status": "delivered",
                                                "timestamp": "1777777777",
                                            }
                                        ],
                                    }
                                }
                            ]
                        }
                    ]
                }
            )
        )

        self.assertTrue(status["handled"])
        self.assertEqual(status["event_type"], "status_callback")
        self.assertEqual(status["statuses"][0]["status"], "delivered")

    def test_whatsapp_adapter_webhook_handles_approval_button_reply(self) -> None:
        response = self.client.post(
            "/integrations/conversations/channels/whatsapp/messages",
            json={
                "channel_thread_id": "phone-number-approval",
                "channel_user_id": "15557654321",
                "text": "Please request approval",
                "content": {
                    "approval_request": {
                        "approval_type": "workflow_execution",
                        "target_type": "workflow",
                        "target_id": "workflow-whatsapp-approval",
                        "summary": "Run protected workflow workflow-whatsapp-approval",
                    }
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        approval_id = response.json()["result"]["approval_request"]["id"]

        with patch(
            "app.services.conversations.channel_webhooks.ChannelWebhookVerificationService.verify",
            new=AsyncMock(return_value={"verified": True, "required": True, "provider": "whatsapp"}),
        ):
            callback = self.client.post(
                "/integrations/conversations/adapters/whatsapp/webhook",
                json={
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "metadata": {"phone_number_id": "phone-number-approval"},
                                    "messages": [
                                        {
                                            "id": "wamid.approval",
                                            "from": "15557654321",
                                            "interactive": {
                                                "button_reply": {
                                                    "id": f"approval:approve:{approval_id}",
                                                    "title": "Approve",
                                                }
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                ]
                },
            )
        self.assertEqual(callback.status_code, 200)
        body = callback.json()
        self.assertTrue(body["handled"])
        self.assertEqual(body["event_type"], "approval_action")
        self.assertEqual(body["provider_outbound_messages"][0]["payload"]["to"], "15557654321")

    def test_delivery_hook_sends_telegram_outbound_messages_with_connector_credential(self) -> None:
        self._create_delivery_user_and_credential(
            user_id="user-telegram-delivery",
            credential_id="credential-telegram-delivery",
            provider="telegram",
            secret_ref="env://TELEGRAM_BOT_TOKEN",
        )
        provider_message = {
            "method": "sendMessage",
            "payload": {"chat_id": "telegram-chat-delivery", "text": "Delivered by hook"},
        }

        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "telegram-token"}, clear=False), patch(
            "app.services.conversations.channel_delivery.httpx.request",
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}),
        ) as request_mock:
            response = self.client.post(
                "/integrations/conversations/adapters/telegram/deliver",
                headers={"x-agency-user-id": "user-telegram-delivery"},
                json={
                    "credential_id": "credential-telegram-delivery",
                    "provider_outbound_messages": [provider_message],
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        request_mock.assert_called_once()
        request_kwargs = request_mock.call_args.kwargs
        self.assertEqual(request_kwargs["url"], "https://api.telegram.org/bottelegram-token/sendMessage")
        self.assertEqual(request_kwargs["json"]["chat_id"], "telegram-chat-delivery")

    def test_delivery_hook_sends_telegram_outbound_messages_with_runtime_secret_mirror(self) -> None:
        installation_id = "installation-telegram-delivery-runtime"
        asyncio.run(
            self.context.connector_installation_repo.create(
                ConnectorInstallation(
                    id=installation_id,
                    owner_user_id="user-telegram-delivery-runtime",
                    provider="telegram",
                    name="Telegram Runtime",
                    onecli_credential_ref=f"secret://agency/installations/{installation_id}",
                    runtime_secret_encrypted=seal_runtime_secret("telegram-token"),
                    status="active",
                    metadata={},
                )
            )
        )
        self._create_delivery_user_and_credential(
            user_id="user-telegram-delivery-runtime",
            credential_id="credential-telegram-delivery-runtime",
            provider="telegram",
            secret_ref=f"secret://agency/installations/{installation_id}",
        )
        provider_message = {
            "method": "sendMessage",
            "payload": {"chat_id": "telegram-chat-delivery-runtime", "text": "Delivered by mirror"},
        }

        with patch("app.services.conversations.channel_delivery.httpx.request",
                   return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}),
        ) as request_mock:
            response = self.client.post(
                "/integrations/conversations/adapters/telegram/deliver",
                headers={"x-agency-user-id": "user-telegram-delivery-runtime"},
                json={
                    "credential_id": "credential-telegram-delivery-runtime",
                    "provider_outbound_messages": [provider_message],
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["credential_mode"], "direct")
        request_mock.assert_called_once()
        request_kwargs = request_mock.call_args.kwargs
        self.assertEqual(
            request_kwargs["url"],
            "https://api.telegram.org/bottelegram-token/sendMessage",
        )
        self.assertEqual(request_kwargs["json"]["chat_id"], "telegram-chat-delivery-runtime")

    def test_delivery_hook_uses_explicit_ca_bundle_for_direct_telegram_delivery(self) -> None:
        self._create_delivery_user_and_credential(
            user_id="user-telegram-delivery-ca",
            credential_id="credential-telegram-delivery-ca",
            provider="telegram",
            secret_ref="env://TELEGRAM_BOT_TOKEN",
        )
        provider_message = {
            "method": "sendMessage",
            "payload": {"chat_id": "telegram-chat-delivery-ca", "text": "Delivered with CA bundle"},
        }

        with patch.dict(
            "os.environ",
            {"TELEGRAM_BOT_TOKEN": "telegram-token", "SSL_CERT_FILE": "/tmp/agency-ca.pem"},
            clear=False,
        ), patch(
            "app.core.tls.macos_direct_ca_bundle",
            return_value="/tmp/agency-direct-ca-merged.pem",
        ), patch(
            "app.services.conversations.channel_delivery.httpx.request",
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}),
        ) as request_mock:
            response = self.client.post(
                "/integrations/conversations/adapters/telegram/deliver",
                headers={"x-agency-user-id": "user-telegram-delivery-ca"},
                json={
                    "credential_id": "credential-telegram-delivery-ca",
                    "provider_outbound_messages": [provider_message],
                },
            )

        self.assertEqual(response.status_code, 200)
        request_kwargs = request_mock.call_args.kwargs
        self.assertEqual(request_kwargs["verify"], "/tmp/agency-direct-ca-merged.pem")
        self.assertEqual(request_kwargs["trust_env"], False)

    def test_delivery_hook_sends_discord_outbound_messages_with_bot_auth(self) -> None:
        self._create_delivery_user_and_credential(
            user_id="user-discord-delivery",
            credential_id="credential-discord-delivery",
            provider="discord",
            secret_ref="env://DISCORD_BOT_TOKEN",
        )
        provider_message = {
            "method": "createMessage",
            "payload": {"channel_id": "discord-channel-delivery", "content": "Delivered by hook"},
        }

        with patch.dict("os.environ", {"DISCORD_BOT_TOKEN": "discord-token"}, clear=False), patch(
            "app.services.conversations.channel_delivery.httpx.request",
            return_value=httpx.Response(200, json={"id": "discord-message-delivery"}),
        ) as request_mock:
            response = self.client.post(
                "/integrations/conversations/adapters/discord/deliver",
                headers={"x-agency-user-id": "user-discord-delivery"},
                json={
                    "credential_id": "credential-discord-delivery",
                    "provider_outbound_messages": [provider_message],
                },
            )

        self.assertEqual(response.status_code, 200)
        request_kwargs = request_mock.call_args.kwargs
        self.assertEqual(
            request_kwargs["url"],
            "https://discord.com/api/v10/channels/discord-channel-delivery/messages",
        )
        self.assertEqual(request_kwargs["headers"], {"Authorization": "Bot discord-token"})
        self.assertNotIn("channel_id", request_kwargs["json"])

    def test_delivery_hook_repairs_missing_discord_credential_projection_from_installation(self) -> None:
        installation_id = "installation-discord-delivery-projection"
        asyncio.run(
            self.context.user_repo.save(
                UserDefinition(
                    id="user-discord-installation-delivery",
                    email="user-discord-installation-delivery@example.com",
                    display_name="Discord Installation Delivery",
                )
            )
        )
        asyncio.run(
            self.context.connector_installation_repo.create(
                ConnectorInstallation(
                    id=installation_id,
                    owner_user_id="user-discord-installation-delivery",
                    provider="discord",
                    name="Discord Installation",
                    onecli_credential_ref=(
                        "onecli://users/user-discord-installation-delivery/"
                        f"discord-bot/{installation_id}"
                    ),
                    status="active",
                    metadata={"default_guild_id": "discord-guild-projection"},
                )
            )
        )
        provider_message = {
            "method": "createMessage",
            "payload": {"channel_id": "discord-channel-projection", "content": "Delivered by projection"},
        }

        with patch.dict(
            "os.environ",
            {
                "ONECLI_ENABLED": "true",
                "ONECLI_GATEWAY_URL": "http://onecli:10255",
                "ONECLI_AGENT_TOKEN_SECRET_REF": "env://ONECLI_AGENT_TOKEN",
                "ONECLI_AGENT_TOKEN": "test-onecli-agent-token",
            },
            clear=False,
        ), patch(
            "app.services.conversations.channel_delivery.httpx.request",
            return_value=httpx.Response(200, json={"id": "discord-message-projection"}),
        ) as request_mock:
            reset_settings_cache()
            response = self.client.post(
                "/integrations/conversations/adapters/discord/deliver",
                headers={"x-agency-user-id": "user-discord-installation-delivery"},
                json={
                    "credential_id": installation_id,
                    "provider_outbound_messages": [provider_message],
                },
            )
            reset_settings_cache()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["credential_id"], installation_id)
        self.assertEqual(body["credential_mode"], "onecli")
        self.assertEqual(
            body["secret_identifier"],
            f"users/user-discord-installation-delivery/discord-bot/{installation_id}",
        )
        repaired = asyncio.run(self.context.credential_repo.get(installation_id))
        self.assertIsNotNone(repaired)
        self.assertEqual(repaired.provider, "discord-bot")
        request_kwargs = request_mock.call_args.kwargs
        self.assertEqual(
            request_kwargs["url"],
            "https://discord.com/api/v10/channels/discord-channel-projection/messages",
        )
        self.assertIsNone(request_kwargs["headers"])
        self.assertNotIn("channel_id", request_kwargs["json"])

    def test_delivery_hook_sends_teams_outbound_messages_with_adaptive_cards(self) -> None:
        self._create_delivery_user_and_credential(
            user_id="user-teams-delivery",
            credential_id="credential-teams-delivery",
            provider="microsoft-teams",
            secret_ref="env://TEAMS_BOT_TOKEN",
            metadata={"team_id": "team-delivery", "channel_id": "channel-delivery"},
        )
        provider_message = create_channel_outbound_formatter("microsoft-teams").format_messages(
            [{"type": "approval", "approval_request_id": "approval-teams-delivery", "text": "Approve this run"}],
            target=AdapterInboundMessage(
                channel_type="microsoft-teams",
                channel_thread_id="channel-delivery",
                channel_user_id="teams-user-delivery",
                channel_display_name="Teams User",
                text="",
                external_message_id=None,
                metadata={"team_id": "team-delivery", "channel_id": "channel-delivery"},
            ),
        )[0]

        with patch.dict("os.environ", {"TEAMS_BOT_TOKEN": "teams-token"}, clear=False), patch(
            "app.services.conversations.channel_delivery.httpx.request",
            return_value=httpx.Response(200, json={"id": "teams-message-delivery"}),
        ) as request_mock:
            response = self.client.post(
                "/integrations/conversations/adapters/microsoft-teams/deliver",
                headers={"x-agency-user-id": "user-teams-delivery"},
                json={
                    "credential_id": "credential-teams-delivery",
                    "provider_outbound_messages": [provider_message],
                },
            )

        self.assertEqual(response.status_code, 200)
        request_kwargs = request_mock.call_args.kwargs
        self.assertEqual(
            request_kwargs["url"],
            "https://graph.microsoft.com/v1.0/teams/team-delivery/channels/channel-delivery/messages",
        )
        self.assertEqual(request_kwargs["headers"], {"Authorization": "Bearer teams-token"})
        self.assertIn("attachments", request_kwargs["json"])
        self.assertEqual(request_kwargs["json"]["attachments"][0]["contentType"], "application/vnd.microsoft.card.adaptive")

    def test_delivery_hook_sends_whatsapp_outbound_messages_with_phone_number_metadata(self) -> None:
        self._create_delivery_user_and_credential(
            user_id="user-whatsapp-delivery",
            credential_id="credential-whatsapp-delivery",
            provider="whatsapp",
            secret_ref="env://WHATSAPP_TOKEN",
            metadata={"phone_number_id": "phone-number-delivery", "api_version": "v21.0"},
        )
        provider_message = {
            "method": "messages",
            "payload": {
                "messaging_product": "whatsapp",
                "to": "15550001111",
                "type": "text",
                "text": {"body": "Delivered by hook"},
            },
        }

        with patch.dict("os.environ", {"WHATSAPP_TOKEN": "whatsapp-token"}, clear=False), patch(
            "app.services.conversations.channel_delivery.httpx.request",
            return_value=httpx.Response(200, json={"messages": [{"id": "wamid.delivery"}]}),
        ) as request_mock:
            response = self.client.post(
                "/integrations/conversations/adapters/whatsapp/deliver",
                headers={"x-agency-user-id": "user-whatsapp-delivery"},
                json={
                    "credential_id": "credential-whatsapp-delivery",
                    "provider_outbound_messages": [provider_message],
                },
            )

        self.assertEqual(response.status_code, 200)
        request_kwargs = request_mock.call_args.kwargs
        self.assertEqual(
            request_kwargs["url"],
            "https://graph.facebook.com/v21.0/phone-number-delivery/messages",
        )
        self.assertEqual(request_kwargs["headers"], {"Authorization": "Bearer whatsapp-token"})
        self.assertEqual(request_kwargs["json"]["to"], "15550001111")

    def test_delivery_hook_sends_slack_outbound_message_with_bearer_auth(self) -> None:
        self._create_delivery_user_and_credential(
            user_id="user-slack-delivery",
            credential_id="credential-slack-delivery",
            provider="slack",
            secret_ref="env://SLACK_BOT_TOKEN",
            metadata={"workspace_id": "workspace-slack"},
        )
        provider_message = {
            "method": "chat.postMessage",
            "payload": {"channel": "slack-channel-delivery", "text": "Delivered by Slack"},
        }

        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "slack-token"}, clear=False), patch(
            "app.services.conversations.channel_delivery.httpx.request",
            return_value=httpx.Response(200, json={"ok": True, "ts": "123.456"}),
        ) as request_mock:
            response = self.client.post(
                "/integrations/conversations/adapters/slack/deliver",
                headers={"x-agency-user-id": "user-slack-delivery"},
                json={
                    "credential_id": "credential-slack-delivery",
                    "provider_outbound_messages": [provider_message],
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        request_mock.assert_called_once()
        request_kwargs = request_mock.call_args.kwargs
        self.assertEqual(request_kwargs["url"], "https://slack.com/api/chat.postMessage")
        self.assertEqual(request_kwargs["headers"], {"Authorization": "Bearer slack-token"})
        self.assertEqual(request_kwargs["json"], {"channel": "slack-channel-delivery", "text": "Delivered by Slack"})

    def test_delivery_hook_sends_twilio_sms_with_basic_auth_and_form_body(self) -> None:
        self._create_delivery_user_and_credential(
            user_id="user-twilio-delivery",
            credential_id="credential-twilio-delivery",
            provider="twilio",
            secret_ref="env://TWILIO_AUTH_TOKEN",
            metadata={"account_sid": "AC123", "from_number": "+15550001000"},
        )
        provider_message = {
            "method": "messages",
            "payload": {"to": "+15550002000", "body": "Delivered by Twilio"},
        }

        with patch.dict("os.environ", {"TWILIO_AUTH_TOKEN": "twilio-token"}, clear=False), patch(
            "app.services.conversations.channel_delivery.httpx.request",
            return_value=httpx.Response(201, json={"sid": "SM123"}),
        ) as request_mock:
            response = self.client.post(
                "/integrations/conversations/adapters/twilio/deliver",
                headers={"x-agency-user-id": "user-twilio-delivery"},
                json={
                    "credential_id": "credential-twilio-delivery",
                    "provider_outbound_messages": [provider_message],
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        request_mock.assert_called_once()
        request_kwargs = request_mock.call_args.kwargs
        self.assertEqual(
            request_kwargs["url"],
            "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages.json",
        )
        self.assertEqual(request_kwargs["auth"], ("AC123", "twilio-token"))
        self.assertEqual(
            request_kwargs["data"],
            {"From": "+15550001000", "To": "+15550002000", "Body": "Delivered by Twilio"},
        )

    def test_delivery_hook_sends_discord_outbound_messages_through_onecli_without_bot_auth(self) -> None:
        self._create_delivery_user_and_credential(
            user_id="user-discord-onecli-delivery",
            credential_id="credential-discord-onecli-delivery",
            provider="discord",
            secret_ref="onecli://discord/dev-bot",
        )
        provider_message = {
            "method": "createMessage",
            "payload": {"channel_id": "discord-channel-onecli", "content": "Delivered by OneCLI"},
        }

        with patch.dict(
            "os.environ",
            {
                "ONECLI_ENABLED": "true",
                "ONECLI_GATEWAY_URL": "http://onecli:10255",
                "ONECLI_AGENT_TOKEN_SECRET_REF": "env://ONECLI_AGENT_TOKEN",
                "ONECLI_AGENT_TOKEN": "test-onecli-agent-token",
            },
            clear=False,
        ), patch(
            "app.services.conversations.channel_delivery.httpx.request",
            return_value=httpx.Response(200, json={"id": "discord-message-onecli"}),
        ) as request_mock:
            reset_settings_cache()
            response = self.client.post(
                "/integrations/conversations/adapters/discord/deliver",
                headers={"x-agency-user-id": "user-discord-onecli-delivery"},
                json={
                    "credential_id": "credential-discord-onecli-delivery",
                    "provider_outbound_messages": [provider_message],
                },
            )
            reset_settings_cache()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["credential_mode"], "onecli")
        self.assertEqual(body["secret_source"], "onecli")
        self.assertEqual(body["secret_identifier"], "discord/dev-bot")
        self.assertEqual(body["onecli"]["gateway_url"], "http://onecli:10255")
        self.assertTrue(body["onecli"]["agent_token_secret_ref_configured"])
        self.assertNotIn("env://ONECLI_AGENT_TOKEN", str(body))

        request_mock.assert_called_once()
        request_kwargs = request_mock.call_args.kwargs
        self.assertEqual(
            request_kwargs["url"],
            "https://discord.com/api/v10/channels/discord-channel-onecli/messages",
        )
        self.assertIsNone(request_kwargs["headers"])
        self.assertEqual(request_kwargs["proxy"], "http://x:test-onecli-agent-token@onecli:10255")
        self.assertNotIn("discord/dev-bot", str(request_kwargs))
        self.assertNotIn("channel_id", request_kwargs["json"])

    def test_delivery_hook_sends_whatsapp_outbound_messages_through_onecli_without_bearer_auth(self) -> None:
        self._create_delivery_user_and_credential(
            user_id="user-whatsapp-onecli-delivery",
            credential_id="credential-whatsapp-onecli-delivery",
            provider="whatsapp",
            secret_ref="onecli://whatsapp/dev-phone",
            metadata={"phone_number_id": "phone-number-onecli", "api_version": "v21.0"},
        )
        provider_message = {
            "method": "messages",
            "payload": {
                "messaging_product": "whatsapp",
                "to": "15550002222",
                "type": "text",
                "text": {"body": "Delivered by OneCLI"},
            },
        }

        with patch.dict(
            "os.environ",
            {
                "ONECLI_ENABLED": "true",
                "ONECLI_GATEWAY_URL": "http://onecli:10255",
            },
            clear=False,
        ), patch(
            "app.services.conversations.channel_delivery.httpx.request",
            return_value=httpx.Response(200, json={"messages": [{"id": "wamid.onecli"}]}),
        ) as request_mock:
            reset_settings_cache()
            response = self.client.post(
                "/integrations/conversations/adapters/whatsapp/deliver",
                headers={"x-agency-user-id": "user-whatsapp-onecli-delivery"},
                json={
                    "credential_id": "credential-whatsapp-onecli-delivery",
                    "provider_outbound_messages": [provider_message],
                },
            )
            reset_settings_cache()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["credential_mode"], "onecli")
        self.assertEqual(body["secret_source"], "onecli")
        self.assertEqual(body["secret_identifier"], "whatsapp/dev-phone")

        request_mock.assert_called_once()
        request_kwargs = request_mock.call_args.kwargs
        self.assertEqual(
            request_kwargs["url"],
            "https://graph.facebook.com/v21.0/phone-number-onecli/messages",
        )
        self.assertIsNone(request_kwargs["headers"])
        proxy = urlparse(request_kwargs["proxy"])
        self.assertEqual(proxy.scheme, "http")
        self.assertEqual(proxy.hostname, "onecli")
        self.assertEqual(proxy.port, 10255)
        self.assertEqual(request_kwargs["json"]["to"], "15550002222")
        self.assertNotIn("whatsapp/dev-phone", str(request_kwargs))

    def test_delivery_hook_uses_telegram_onecli_url_path_injection(self) -> None:
        self._create_delivery_user_and_credential(
            user_id="user-telegram-onecli-delivery",
            credential_id="credential-telegram-onecli-delivery",
            provider="telegram",
            secret_ref="onecli://telegram/dev-bot",
        )

        with patch.dict(
            "os.environ",
            {
                "ONECLI_ENABLED": "true",
                "ONECLI_GATEWAY_URL": "http://onecli:10255",
            },
            clear=False,
        ), patch(
            "app.services.conversations.channel_delivery.httpx.request",
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}),
        ) as request_mock:
            reset_settings_cache()
            response = self.client.post(
                "/integrations/conversations/adapters/telegram/deliver",
                headers={"x-agency-user-id": "user-telegram-onecli-delivery"},
                json={
                    "credential_id": "credential-telegram-onecli-delivery",
                    "provider_outbound_messages": [
                        {
                            "method": "sendMessage",
                            "payload": {"chat_id": "telegram-chat-onecli", "text": "No token URL"},
                        }
                    ],
                },
            )
            reset_settings_cache()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        request_mock.assert_called_once()
        request_kwargs = request_mock.call_args.kwargs
        self.assertEqual(
            request_kwargs["url"],
            "https://api.telegram.org/botonecli-managed/sendMessage",
        )
        self.assertIn("onecli", request_kwargs["proxy"])
        self.assertNotIn("dev-bot", str(request_kwargs))

    def test_delivery_hook_rejects_cross_provider_credential(self) -> None:
        self._create_delivery_user_and_credential(
            user_id="user-delivery-mismatch",
            credential_id="credential-delivery-mismatch",
            provider="telegram",
            secret_ref="env://TELEGRAM_BOT_TOKEN",
        )

        response = self.client.post(
            "/integrations/conversations/adapters/discord/deliver",
            headers={"x-agency-user-id": "user-delivery-mismatch"},
            json={
                "credential_id": "credential-delivery-mismatch",
                "provider_outbound_messages": [
                    {
                        "method": "createMessage",
                        "payload": {"channel_id": "discord-channel", "content": "Nope"},
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("not configured for discord", response.json()["detail"])

    def test_conversation_bound_delivery_sends_discord_message_to_saved_channel(self) -> None:
        self._create_delivery_user_and_credential(
            user_id="user-discord-conversation-delivery",
            credential_id="credential-discord-conversation-delivery",
            provider="discord",
            secret_ref="env://DISCORD_BOT_TOKEN",
        )
        conversation_response = self.client.post(
            "/integrations/conversations/channels/discord/resolve",
            json={
                "channel_thread_id": "discord-channel-bound",
                "channel_user_id": "discord-user-bound",
                "channel_display_name": "Discord User",
                "metadata": {"guild_id": "guild-bound"},
            },
        )
        self.assertEqual(conversation_response.status_code, 200)
        conversation_id = conversation_response.json()["id"]

        with patch.dict("os.environ", {"DISCORD_BOT_TOKEN": "discord-token"}, clear=False), patch(
            "app.services.conversations.channel_delivery.httpx.request",
            return_value=httpx.Response(200, json={"id": "discord-message-bound"}),
        ) as request_mock:
            response = self.client.post(
                f"/integrations/conversations/channels/{conversation_id}/deliver",
                headers={"x-agency-user-id": "user-discord-conversation-delivery"},
                json={
                    "credential_id": "credential-discord-conversation-delivery",
                    "outbound_messages": [{"type": "text", "text": "Bound Discord delivery"}],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        request_kwargs = request_mock.call_args.kwargs
        self.assertEqual(
            request_kwargs["url"],
            "https://discord.com/api/v10/channels/discord-channel-bound/messages",
        )
        self.assertEqual(request_kwargs["headers"], {"Authorization": "Bot discord-token"})
        self.assertEqual(request_kwargs["json"], {"content": "Bound Discord delivery"})

    def test_conversation_bound_delivery_sends_telegram_message_to_saved_chat(self) -> None:
        self._create_delivery_user_and_credential(
            user_id="user-telegram-conversation-delivery",
            credential_id="credential-telegram-conversation-delivery",
            provider="telegram",
            secret_ref="env://TELEGRAM_BOT_TOKEN",
        )
        conversation_response = self.client.post(
            "/integrations/conversations/channels/telegram/resolve",
            json={
                "channel_thread_id": "telegram-chat-bound",
                "channel_user_id": "telegram-user-bound",
            },
        )
        self.assertEqual(conversation_response.status_code, 200)
        conversation_id = conversation_response.json()["id"]

        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "telegram-token"}, clear=False), patch(
            "app.services.conversations.channel_delivery.httpx.request",
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}),
        ) as request_mock:
            response = self.client.post(
                f"/integrations/conversations/channels/{conversation_id}/deliver",
                headers={"x-agency-user-id": "user-telegram-conversation-delivery"},
                json={
                    "credential_id": "credential-telegram-conversation-delivery",
                    "outbound_messages": [{"type": "text", "text": "Bound Telegram delivery"}],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        request_kwargs = request_mock.call_args.kwargs
        self.assertEqual(
            request_kwargs["url"],
            "https://api.telegram.org/bottelegram-token/sendMessage",
        )
        self.assertEqual(
            request_kwargs["json"],
            {"chat_id": "telegram-chat-bound", "text": "Bound Telegram delivery"},
        )

    def test_conversation_bound_delivery_sends_whatsapp_message_to_saved_recipient(self) -> None:
        self._create_delivery_user_and_credential(
            user_id="user-whatsapp-conversation-delivery",
            credential_id="credential-whatsapp-conversation-delivery",
            provider="whatsapp",
            secret_ref="env://WHATSAPP_ACCESS_TOKEN",
            metadata={"phone_number_id": "phone-number-bound", "api_version": "v21.0"},
        )
        conversation_response = self.client.post(
            "/integrations/conversations/channels/whatsapp/resolve",
            json={
                "channel_user_id": "15551234567",
                "channel_display_name": "WhatsApp Recipient",
            },
        )
        self.assertEqual(conversation_response.status_code, 200)
        conversation_id = conversation_response.json()["id"]

        with patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": "whatsapp-token"}, clear=False), patch(
            "app.services.conversations.channel_delivery.httpx.request",
            return_value=httpx.Response(200, json={"messages": [{"id": "wamid.bound"}]}),
        ) as request_mock:
            response = self.client.post(
                f"/integrations/conversations/channels/{conversation_id}/deliver",
                headers={"x-agency-user-id": "user-whatsapp-conversation-delivery"},
                json={
                    "credential_id": "credential-whatsapp-conversation-delivery",
                    "outbound_messages": [{"type": "text", "text": "Bound WhatsApp delivery"}],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        request_kwargs = request_mock.call_args.kwargs
        self.assertEqual(
            request_kwargs["url"],
            "https://graph.facebook.com/v21.0/phone-number-bound/messages",
        )
        self.assertEqual(request_kwargs["headers"], {"Authorization": "Bearer whatsapp-token"})
        self.assertEqual(request_kwargs["json"]["to"], "15551234567")
        self.assertEqual(request_kwargs["json"]["text"]["body"], "Bound WhatsApp delivery")

    def test_transport_adapter_renders_approval_actions(self) -> None:
        response = self.client.post(
            "/integrations/conversations/channels/telegram/messages",
            json={
                "channel_thread_id": "thread-2",
                "channel_user_id": "telegram-user-2",
                "channel_display_name": "Telegram User 2",
                "text": "Please request approval",
                "content": {
                    "approval_request": {
                        "approval_type": "workflow_execution",
                        "target_type": "workflow",
                        "target_id": "workflow-1",
                        "summary": "Run protected workflow workflow-1",
                    }
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["outbound_messages"][0]["type"], "approval")
        self.assertEqual(payload["outbound_messages"][0]["actions"][0]["type"], "approve")

        approved = self.client.post(
            "/integrations/conversations/channels/telegram/approval-actions",
            json={
                "channel_thread_id": "thread-2",
                "channel_user_id": "telegram-user-2",
                "approval_request_id": payload["result"]["approval_request"]["id"],
                "action": "approve",
                "reason": "Proceed",
            },
        )
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["outbound_messages"][0]["type"], "text")
        self.assertIn("Approval granted", approved.json()["outbound_messages"][0]["text"])

    def test_unmapped_external_identity_cannot_create_workflow_via_transport(self) -> None:
        response = self.client.post(
            "/integrations/conversations/channels/telegram/messages",
            json={
                "channel_thread_id": "thread-3",
                "channel_user_id": "telegram-user-3",
                "internal_user_id": "user-spoofed",
                "text": "Create a workflow",
                "content": {
                    "workflow_proposal": {
                        "workflow": {
                            "id": "workflow-external",
                            "name": "External Workflow",
                            "entrypoint": "node-1",
                            "nodes": [{"id": "node-1", "name": "Entry", "node_type": "task", "task_id": "task-1"}],
                            "task_definitions": [{"id": "task-1", "name": "Task One", "description": "Do work"}],
                            "metadata": {"visible_to_main_agent": True, "mutable_by_main_agent": True},
                        }
                    }
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["outbound_messages"][0]["text"],
            "This channel is not allowed to create or update workflows without a trusted mapped identity.",
        )

    def test_trusted_channel_identity_mapping_can_create_workflow_via_transport(self) -> None:
        asyncio.run(
            self.context.user_repo.save(
                UserDefinition(id="user-mapped", email="mapped@example.com", display_name="Mapped User")
            )
        )
        mapping = self.client.post(
            "/integrations/conversations/channel-identity-mappings",
            headers={"x-agency-user-id": "user-mapped"},
            json={
                "channel_type": "telegram",
                "channel_user_id": "telegram-user-mapped",
                "internal_user_id": "user-mapped",
                "channel_display_name": "Mapped Telegram User",
                "trusted": True,
            },
        )
        self.assertEqual(mapping.status_code, 200)
        self.assertEqual(mapping.json()["internal_user_id"], "user-mapped")

        response = self.client.post(
            "/integrations/conversations/channels/telegram/messages",
            json={
                "channel_thread_id": "thread-mapped",
                "channel_user_id": "telegram-user-mapped",
                "text": "Create a workflow",
                "content": {
                    "workflow_proposal": {
                        "workflow": {
                            "id": "workflow-mapped-transport",
                            "name": "Mapped Transport Workflow",
                            "entrypoint": "node-1",
                            "nodes": [{"id": "node-1", "name": "Entry", "node_type": "task", "task_id": "task-1"}],
                            "task_definitions": [{"id": "task-1", "name": "Task One", "description": "Do work"}],
                            "metadata": {"visible_to_main_agent": True, "mutable_by_main_agent": True},
                        }
                    }
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["outbound_messages"][0]["type"], "approval")
        self.assertEqual(payload["conversation"]["created_by_user_id"], "user-mapped")

        approved = self.client.post(
            "/integrations/conversations/channels/telegram/approval-actions",
            json={
                "channel_thread_id": "thread-mapped",
                "channel_user_id": "telegram-user-mapped",
                "approval_request_id": payload["result"]["approval_request"]["id"],
                "action": "approve",
                "reason": "Approved",
            },
        )
        self.assertEqual(approved.status_code, 200)
        workflow = asyncio.run(self.context.workflow_repo.get("workflow-mapped-transport"))
        assert workflow is not None
        self.assertEqual(workflow.name, "Mapped Transport Workflow")

    def test_duplicate_external_workflow_proposal_replays_existing_approval(self) -> None:
        asyncio.run(
            self.context.user_repo.save(
                UserDefinition(id="user-idempotent", email="idempotent@example.com", display_name="Idempotent User")
            )
        )
        mapping = self.client.post(
            "/integrations/conversations/channel-identity-mappings",
            headers={"x-agency-user-id": "user-idempotent"},
            json={
                "channel_type": "telegram",
                "channel_user_id": "telegram-user-idempotent-workflow",
                "internal_user_id": "user-idempotent",
                "trusted": True,
            },
        )
        self.assertEqual(mapping.status_code, 200)
        request_payload = {
            "channel_thread_id": "thread-idempotent-workflow",
            "channel_user_id": "telegram-user-idempotent-workflow",
            "text": "Create a workflow",
            "external_message_id": "tg-idempotent-workflow-1",
            "content": {
                "workflow_proposal": {
                    "workflow": {
                        "id": "workflow-idempotent-transport",
                        "name": "Idempotent Transport Workflow",
                        "entrypoint": "node-1",
                        "nodes": [{"id": "node-1", "name": "Entry", "node_type": "task", "task_id": "task-1"}],
                        "task_definitions": [{"id": "task-1", "name": "Task One", "description": "Do work"}],
                        "metadata": {"visible_to_main_agent": True, "mutable_by_main_agent": True},
                    }
                }
            },
        }

        first = self.client.post("/integrations/conversations/channels/telegram/messages", json=request_payload)
        self.assertEqual(first.status_code, 200)
        conversation_id = first.json()["conversation"]["id"]
        first_approval_id = first.json()["outbound_messages"][0]["approval_request_id"]

        second = self.client.post("/integrations/conversations/channels/telegram/messages", json=request_payload)
        self.assertEqual(second.status_code, 200)
        replay = second.json()
        self.assertTrue(replay["result"]["idempotent"])
        self.assertEqual(replay["outbound_messages"][0]["approval_request_id"], first_approval_id)

        approvals = asyncio.run(self.context.conversation_approval_repo.list_by_conversation(conversation_id))
        messages = asyncio.run(self.context.conversation_message_repo.list_by_conversation(conversation_id))
        self.assertEqual(len(approvals), 1)
        self.assertEqual(len(messages), 2)

    def test_untrusted_channel_identity_mapping_cannot_create_workflow_via_transport(self) -> None:
        asyncio.run(
            self.context.user_repo.save(
                UserDefinition(id="user-untrusted", email="untrusted@example.com", display_name="Untrusted User")
            )
        )
        mapping = self.client.post(
            "/integrations/conversations/channel-identity-mappings",
            headers={"x-agency-user-id": "user-untrusted"},
            json={
                "channel_type": "telegram",
                "channel_user_id": "telegram-user-untrusted",
                "internal_user_id": "user-untrusted",
                "trusted": False,
            },
        )
        self.assertEqual(mapping.status_code, 200)

        response = self.client.post(
            "/integrations/conversations/channels/telegram/messages",
            json={
                "channel_thread_id": "thread-untrusted",
                "channel_user_id": "telegram-user-untrusted",
                "text": "Create a workflow",
                "content": {
                    "workflow_proposal": {
                        "workflow": {
                            "id": "workflow-untrusted-transport",
                            "name": "Untrusted Transport Workflow",
                            "entrypoint": "node-1",
                            "nodes": [{"id": "node-1", "name": "Entry", "node_type": "task", "task_id": "task-1"}],
                            "task_definitions": [{"id": "task-1", "name": "Task One", "description": "Do work"}],
                            "metadata": {"visible_to_main_agent": True, "mutable_by_main_agent": True},
                        }
                    }
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["outbound_messages"][0]["text"],
            "This channel is not allowed to create or update workflows without a trusted mapped identity.",
        )
        workflow = asyncio.run(self.context.workflow_repo.get("workflow-untrusted-transport"))
        self.assertIsNone(workflow)

    def test_unmapped_channel_approval_is_bound_to_the_same_external_identity(self) -> None:
        response = self.client.post(
            "/integrations/conversations/channels/telegram/messages",
            json={
                "channel_thread_id": "thread-anon-approval",
                "channel_user_id": "telegram-user-anon",
                "text": "Please request approval",
                "content": {
                    "approval_request": {
                        "approval_type": "workflow_execution",
                        "target_type": "workflow",
                        "target_id": "workflow-anon-approval",
                        "summary": "Run protected workflow workflow-anon-approval",
                    }
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        approval_id = response.json()["result"]["approval_request"]["id"]

        rejected = self.client.post(
            "/integrations/conversations/channels/telegram/approval-actions",
            json={
                "channel_thread_id": "thread-anon-approval",
                "channel_user_id": "telegram-user-other",
                "approval_request_id": approval_id,
                "action": "approve",
                "reason": "Not mine",
            },
        )
        self.assertEqual(rejected.status_code, 403)
        self.assertIn("may not resolve approval request", rejected.json()["detail"])

        approved = self.client.post(
            "/integrations/conversations/channels/telegram/approval-actions",
            json={
                "channel_thread_id": "thread-anon-approval",
                "channel_user_id": "telegram-user-anon",
                "approval_request_id": approval_id,
                "action": "approve",
                "reason": "Proceed",
            },
        )
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["result"]["approval_request"]["approved_by_user_id"], "external:telegram-user-anon")

    def test_channel_identity_mapping_api_lists_updates_and_deletes_mappings(self) -> None:
        asyncio.run(
            self.context.user_repo.save(
                UserDefinition(id="user-admin", email="admin@example.com", display_name="Admin User")
            )
        )
        created = self.client.post(
            "/integrations/conversations/channel-identity-mappings",
            headers={"x-agency-user-id": "user-admin"},
            json={
                "channel_type": "discord",
                "channel_user_id": "discord-user-1",
                "internal_user_id": "user-admin",
                "channel_display_name": "Discord User",
                "trusted": True,
            },
        )
        self.assertEqual(created.status_code, 200)
        mapping_id = created.json()["id"]

        updated = self.client.post(
            "/integrations/conversations/channel-identity-mappings",
            headers={"x-agency-user-id": "user-admin"},
            json={
                "channel_type": "discord",
                "channel_user_id": "discord-user-1",
                "internal_user_id": "user-admin",
                "channel_display_name": "Renamed Discord User",
                "trusted": False,
            },
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["id"], mapping_id)
        self.assertFalse(updated.json()["trusted"])
        self.assertEqual(updated.json()["channel_display_name"], "Renamed Discord User")

        listed = self.client.get(
            "/integrations/conversations/channel-identity-mappings",
            headers={"x-agency-user-id": "user-admin"},
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()["items"]), 1)

        deleted = self.client.delete(
            f"/integrations/conversations/channel-identity-mappings/{mapping_id}",
            headers={"x-agency-user-id": "user-admin"},
        )
        self.assertEqual(deleted.status_code, 200)

        listed_again = self.client.get(
            "/integrations/conversations/channel-identity-mappings",
            headers={"x-agency-user-id": "user-admin"},
        )
        self.assertEqual(listed_again.status_code, 200)
        self.assertEqual(listed_again.json()["items"], [])

    def test_trusted_internal_identity_can_create_workflow_via_transport(self) -> None:
        response = self.client.post(
            "/integrations/conversations/channels/web/messages",
            json={
                "channel_thread_id": "thread-4",
                "channel_user_id": "web-user-1",
                "internal_user_id": "user-1",
                "text": "Create a workflow",
                "content": {
                    "workflow_proposal": {
                        "workflow": {
                            "id": "workflow-transport",
                            "name": "Transport Workflow",
                            "entrypoint": "node-1",
                            "nodes": [{"id": "node-1", "name": "Entry", "node_type": "task", "task_id": "task-1"}],
                            "task_definitions": [{"id": "task-1", "name": "Task One", "description": "Do work"}],
                            "metadata": {"visible_to_main_agent": True, "mutable_by_main_agent": True},
                        }
                    }
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["outbound_messages"][0]["type"], "approval")

        approved = self.client.post(
            "/integrations/conversations/channels/web/approval-actions",
            json={
                "channel_thread_id": "thread-4",
                "channel_user_id": "web-user-1",
                "internal_user_id": "user-1",
                "approval_request_id": payload["result"]["approval_request"]["id"],
                "action": "approve",
                "reason": "Approved",
            },
        )
        self.assertEqual(approved.status_code, 200)
        workflow = asyncio.run(self.context.workflow_repo.get("workflow-transport"))
        assert workflow is not None
        self.assertEqual(workflow.name, "Transport Workflow")

    def test_transport_adapter_can_request_protected_execution_approval(self) -> None:
        asyncio.run(
            self.context.workflow_repo.save(
                WorkflowDefinition(
                    id="workflow-protected",
                    name="Protected Workflow",
                    entrypoint="node-1",
                    metadata={"visible_to_main_agent": True, "protected_execution": True},
                )
            )
        )
        response = self.client.post(
            "/integrations/conversations/channels/web/messages",
            json={
                "channel_thread_id": "thread-5",
                "channel_user_id": "web-user-2",
                "internal_user_id": "user-2",
                "text": "Run the protected workflow",
                "content": {"execution_request": {"workflow_id": "workflow-protected"}},
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["outbound_messages"][0]["type"], "approval")

    def test_trusted_channel_identity_can_update_workflow_via_transport(self) -> None:
        self._trust_channel_user(
            user_id="user-workflow-update",
            channel_type="telegram",
            channel_user_id="telegram-user-workflow-update",
            channel_display_name="Workflow Updater",
        )
        asyncio.run(
            self.context.workflow_repo.save(
                WorkflowDefinition(
                    id="workflow-update-transport",
                    name="Workflow Update Transport",
                    description="Original description",
                    entrypoint="node-1",
                    metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
                    versioning={
                        "version": "1.0.0",
                        "revision": 1,
                        "parent_version": None,
                        "is_published": True,
                        "labels": [],
                    },
                )
            )
        )

        requested = self.client.post(
            "/integrations/conversations/channels/telegram/messages",
            json={
                "channel_thread_id": "thread-workflow-update",
                "channel_user_id": "telegram-user-workflow-update",
                "text": "Update the workflow",
                "content": {
                    "workflow_update_proposal": {
                        "workflow_id": "workflow-update-transport",
                        "summary": "Update workflow via transport.",
                        "workflow": {
                            "id": "workflow-update-transport",
                            "name": "Workflow Update Transport",
                            "description": "Updated through transport",
                            "entrypoint": "node-1",
                            "nodes": [{"id": "node-1", "name": "Entry", "node_type": "task", "task_id": "task-1"}],
                            "task_definitions": [{"id": "task-1", "name": "Task One", "description": "Do work"}],
                            "metadata": {"visible_to_main_agent": True, "mutable_by_main_agent": True},
                        },
                    }
                },
            },
        )
        self.assertEqual(requested.status_code, 200)
        self.assertEqual(requested.json()["outbound_messages"][0]["type"], "approval")

        approved = self.client.post(
            "/integrations/conversations/channels/telegram/approval-actions",
            json={
                "channel_thread_id": "thread-workflow-update",
                "channel_user_id": "telegram-user-workflow-update",
                "approval_request_id": requested.json()["result"]["approval_request"]["id"],
                "action": "approve",
                "reason": "Ship it",
            },
        )
        self.assertEqual(approved.status_code, 200)
        workflow = asyncio.run(self.context.workflow_repo.get("workflow-update-transport"))
        assert workflow is not None
        self.assertEqual(workflow.description, "Updated through transport")

    def test_trusted_channel_identity_can_update_agent_via_transport(self) -> None:
        self._trust_channel_user(
            user_id="user-agent-update",
            channel_type="telegram",
            channel_user_id="telegram-user-agent-update",
            channel_display_name="Agent Updater",
        )
        asyncio.run(
            self.context.agent_repo.save(
                AgentDefinition(
                    id="agent-update-transport",
                    name="Agent Update Transport",
                    description="Original description",
                    role="Original role",
                    instructions="Original instructions",
                )
            )
        )

        requested = self.client.post(
            "/integrations/conversations/channels/telegram/messages",
            json={
                "channel_thread_id": "thread-agent-update",
                "channel_user_id": "telegram-user-agent-update",
                "text": "Update the agent",
                "content": {
                    "agent_update_proposal": {
                        "agent_id": "agent-update-transport",
                        "patch": {
                            "description": "Updated through transport",
                            "instructions": "Updated instructions through transport",
                        },
                    }
                },
            },
        )
        self.assertEqual(requested.status_code, 200)
        self.assertEqual(requested.json()["outbound_messages"][0]["type"], "approval")

        approved = self.client.post(
            "/integrations/conversations/channels/telegram/approval-actions",
            json={
                "channel_thread_id": "thread-agent-update",
                "channel_user_id": "telegram-user-agent-update",
                "approval_request_id": requested.json()["result"]["approval_request"]["id"],
                "action": "approve",
                "reason": "Apply it",
            },
        )
        self.assertEqual(approved.status_code, 200)
        agent = asyncio.run(self.context.agent_repo.get("agent-update-transport"))
        assert agent is not None
        self.assertEqual(agent.description, "Updated through transport")
        self.assertEqual(agent.instructions, "Updated instructions through transport")

    def test_trusted_channel_identity_can_propose_tool_update_via_main_agent_tool(self) -> None:
        self._trust_channel_user(
            user_id="user-tool-update",
            channel_type="telegram",
            channel_user_id="telegram-user-tool-update",
            channel_display_name="Tool Updater",
        )
        asyncio.run(
            self.context.tool_repo.create(
                ToolDefinition(
                    id="tool-update-transport",
                    name="tool_update_transport",
                    description="Original tool description",
                    tool_type=ToolType.PYTHON_FUNCTION,
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    output_schema={"type": "object"},
                    implementation=ToolImplementationReference(
                        implementation_type="python_function",
                        target="tests.native_test_tools",
                        callable_name="echo_tool",
                    ),
                    security=SecuritySettings(),
                    mcp_exposure=MCPExposureSettings(),
                )
            )
        )
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-transport-tool-update",
                        name="ProposeToolUpdate",
                        arguments={
                            "tool_id": "tool-update-transport",
                            "summary": "Update tool via transport.",
                            "tool": {
                                "id": "tool-update-transport",
                                "name": "tool_update_transport",
                                "description": "Updated by channel transport",
                                "tool_type": "python_function",
                                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                                "output_schema": {"type": "object"},
                                "implementation": {
                                    "implementation_type": "python_function",
                                    "target": "tests.native_test_tools",
                                    "callable_name": "echo_tool",
                                },
                                "security": {},
                                "mcp_exposure": {},
                            },
                        },
                    )
                ],
                provider="fake",
                model="fake-model",
            )
        ]

        requested = self.client.post(
            "/integrations/conversations/channels/telegram/messages",
            json={
                "channel_thread_id": "thread-tool-update",
                "channel_user_id": "telegram-user-tool-update",
                "text": "On this tool page, propose updating the selected tool description.",
                "metadata": {
                    "page_context": {
                        "surface": "tools.contracts",
                        "selection": {"toolId": "tool-update-transport"},
                        "entities": [{"type": "tool", "id": "tool-update-transport", "label": "tool_update_transport"}],
                    },
                    "assistant_providers": {
                        "version": "2026-05-27",
                        "providers": [
                            {
                                "id": "tool.provider",
                                "label": "Tool provider",
                                "systemToolIds": ["agency.tool.get", "agency.tool.propose-update"],
                                "selection": {"toolId": "tool-update-transport"},
                            }
                        ],
                    },
                },
            },
        )
        self.assertEqual(requested.status_code, 200)
        self.assertEqual(requested.json()["outbound_messages"][0]["type"], "approval")

        approved = self.client.post(
            "/integrations/conversations/channels/telegram/approval-actions",
            json={
                "channel_thread_id": "thread-tool-update",
                "channel_user_id": "telegram-user-tool-update",
                "approval_request_id": requested.json()["result"]["approval_request"]["id"],
                "action": "approve",
                "reason": "Apply it",
            },
        )
        self.assertEqual(approved.status_code, 200)
        tool = asyncio.run(self.context.tool_repo.get("tool-update-transport"))
        assert tool is not None
        self.assertEqual(tool.description, "Updated by channel transport")

    def test_trusted_channel_identity_can_inspect_visible_workflow_via_transport(self) -> None:
        self._trust_channel_user(
            user_id="user-workflow-inspect-transport",
            channel_type="telegram",
            channel_user_id="telegram-user-workflow-inspect-transport",
            channel_display_name="Workflow Inspector",
        )
        asyncio.run(
            self.context.workflow_repo.save(
                WorkflowDefinition(
                    id="workflow-visible-transport",
                    name="Visible Transport Workflow",
                    description="Workflow details exposed through chat transport.",
                    entrypoint="manual",
                    metadata={"visible_to_main_agent": True},
                )
            )
        )
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-get-workflow-transport",
                        name="GetWorkflow",
                        arguments={"workflow_id": "workflow-visible-transport"},
                    )
                ],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(content="I found the workflow.", provider="fake", model="fake-model"),
        ]

        response = self.client.post(
            "/integrations/conversations/channels/telegram/messages",
            json={
                "channel_thread_id": "thread-workflow-inspect-transport",
                "channel_user_id": "telegram-user-workflow-inspect-transport",
                "text": "Inspect workflow",
                "external_message_id": "telegram-workflow-inspect-transport-1",
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["outbound_messages"][0]["type"], "text")
        self.assertEqual(payload["outbound_messages"][0]["text"], "I found the workflow.")
        messages = asyncio.run(
            self.context.conversation_message_repo.list_by_conversation(payload["conversation"]["id"])
        )
        self.assertEqual(messages[1].content["tool_name"], "get_workflow")
        self.assertEqual(messages[2].content["result"]["status"], "ok")
        self.assertEqual(messages[2].content["result"]["workflow"]["id"], "workflow-visible-transport")

    def test_trusted_channel_identity_can_invoke_published_persona_via_transport(self) -> None:
        self._trust_channel_user(
            user_id="user-persona-transport",
            channel_type="telegram",
            channel_user_id="telegram-user-persona-transport",
            channel_display_name="Persona User",
        )
        published = self._publish_minimal_persona(
            user_id="user-persona-transport",
            persona_name="Transport Persona",
            memory_id="persona-transport-source-memory",
        )

        response = self.client.post(
            "/integrations/conversations/channels/telegram/messages",
            json={
                "channel_thread_id": "thread-persona-transport",
                "channel_user_id": "telegram-user-persona-transport",
                "text": "@transport-persona review this observation",
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["result"]["persona"]["id"], published["persona"]["id"])
        self.assertEqual(payload["result"]["assistant_message"]["metadata"]["delivery"], "persona")
        self.assertEqual(payload["result"]["assistant_message"]["metadata"]["persona_slug"], "transport-persona")

    def test_trusted_channel_identity_can_request_tool_execution_approval_via_transport(self) -> None:
        self._trust_channel_user(
            user_id="user-tool-exec-transport",
            channel_type="telegram",
            channel_user_id="telegram-user-tool-exec-transport",
            channel_display_name="Tool Executor",
        )
        asyncio.run(
            self.context.tool_repo.create(
                ToolDefinition(
                    id="tool-click-transport",
                    name="click",
                    description="Computer use click",
                    tool_type=ToolType.PYTHON_FUNCTION,
                    input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                    output_schema={"type": "object"},
                    implementation=ToolImplementationReference(
                        implementation_type="python_function",
                        target="tests.native_test_tools",
                        callable_name="echo_tool",
                        config={"tool_family": "computer_use", "canonical_tool_name": "click"},
                    ),
                    security=SecuritySettings(
                        requires_approval=True,
                        function_allowlist=["echo_tool"],
                    ),
                    mcp_exposure=MCPExposureSettings(),
                    tags=["computer_use"],
                )
            )
        )
        profile = asyncio.run(self.context.main_agent_profile_repo.get("main-agent-profile"))
        assert profile is not None
        agent = asyncio.run(self.context.agent_repo.get(profile.agent_id))
        assert agent is not None
        asyncio.run(self.context.agent_repo.update(agent.id, {"tool_ids": [*agent.tool_ids, "tool-click-transport"]}))
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[ModelToolCall(id="tool-exec-transport-1", name="click", arguments={"text": "clicked"})],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(content="Click completed.", provider="fake", model="fake-model"),
        ]

        requested = self.client.post(
            "/integrations/conversations/channels/telegram/messages",
            json={
                "channel_thread_id": "thread-tool-exec",
                "channel_user_id": "telegram-user-tool-exec-transport",
                "text": "Click there",
            },
        )
        self.assertEqual(requested.status_code, 200)
        self.assertEqual(requested.json()["outbound_messages"][0]["type"], "approval")
        self.assertEqual(requested.json()["result"]["approval_request"]["approval_type"], "tool_execute")

        approved = self.client.post(
            "/integrations/conversations/channels/telegram/approval-actions",
            json={
                "channel_thread_id": "thread-tool-exec",
                "channel_user_id": "telegram-user-tool-exec-transport",
                "approval_request_id": requested.json()["result"]["approval_request"]["id"],
                "action": "approve",
                "reason": "Run it",
            },
        )
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["result"]["approval_request"]["status"], "approved")
        self.assertEqual(approved.json()["result"]["tool_result"], {"echo": "clicked"})


if __name__ == "__main__":
    unittest.main()
