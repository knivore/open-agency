from __future__ import annotations

import asyncio
import hashlib
import hmac
import httpx
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.domain import CredentialDefinition, ModelProfileDefinition, UserDefinition, WorkflowDefinition
from app.llm.base import ModelResponse
from app.llm.registry import LLMEnvironmentConfig
from app.services.conversations.channel_adapters import AdapterInboundMessage, create_channel_outbound_formatter
from app.services.main_agent_setup import MainAgentSetupConfig, MainAgentSetupService


class _FakeModelClient:
    provider_key = "fake"

    def __init__(self, profile: ModelProfileDefinition, env: LLMEnvironmentConfig):
        self.profile = profile
        self.env = env

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(content="direct reply", provider="fake", model=self.profile.model)

    def generate_structured(self, messages, *, schema, temperature=None, max_tokens=None, **kwargs):
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
        self.client = TestClient(create_app(context=self.context))

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
        payload = {
            "update_id": 1001,
            "message": {
                "message_id": 2002,
                "chat": {"id": 3003, "type": "private"},
                "from": {"id": 4004, "username": "telegram_user"},
                "text": "Hello through Telegram adapter",
            },
        }
        first = self.client.post("/integrations/conversations/adapters/telegram/webhook", json=payload)
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertTrue(body["handled"])
        self.assertEqual(body["adapter"], "telegram")
        self.assertEqual(body["event_type"], "message")
        self.assertEqual(body["conversation"]["channel_user_id"], "4004")
        self.assertEqual(body["outbound_messages"][0]["type"], "text")
        self.assertEqual(body["provider_outbound_messages"][0]["method"], "sendMessage")
        self.assertEqual(body["provider_outbound_messages"][0]["payload"]["chat_id"], "3003")

        second = self.client.post("/integrations/conversations/adapters/telegram/webhook", json=payload)
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

    def test_adapter_webhook_requires_connector_credential_in_production(self) -> None:
        payload = {
            "update_id": 1201,
            "message": {
                "message_id": 2202,
                "chat": {"id": 3203, "type": "private"},
                "from": {"id": 4204, "username": "telegram_production"},
                "text": "Unsigned production webhook",
            },
        }

        with patch(
            "app.services.conversations.channel_webhooks.get_settings",
            return_value=SimpleNamespace(app_env="production"),
        ):
            response = self.client.post("/integrations/conversations/adapters/telegram/webhook", json=payload)

        self.assertEqual(response.status_code, 422)
        self.assertIn("connector credential is required", response.json()["detail"])

    def test_telegram_adapter_webhook_handles_approval_callback(self) -> None:
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

        callback = self.client.post(
            "/integrations/conversations/adapters/telegram/webhook",
            json={
                "update_id": 1002,
                "callback_query": {
                    "id": "callback-1",
                    "from": {"id": "telegram-approval-user", "username": "approver"},
                    "data": f"approval:approve:{approval_id}",
                },
            },
        )
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


if __name__ == "__main__":
    unittest.main()
