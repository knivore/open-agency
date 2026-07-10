from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass
from typing import Callable

from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.domain import ModelProfileDefinition
from app.llm.base import ModelResponse
from app.llm.registry import LLMEnvironmentConfig
from app.services.conversations.channel_adapters import AdapterInboundMessage, create_channel_outbound_formatter, create_chat_channel_adapter
from app.services.main_agent_setup.service import MainAgentSetupConfig, MainAgentSetupService


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


@dataclass(frozen=True, slots=True)
class _ProviderCase:
    provider: str
    thread_id: str
    user_id: str
    display_name: str
    message_payload: dict
    callback_payload: Callable[[str], dict]


class MultichannelSharedContractTests(unittest.TestCase):
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

    def test_inbound_adapters_produce_the_same_required_conversation_shape(self) -> None:
        for case in self._provider_cases():
            adapter = create_chat_channel_adapter(self.context, case.provider)
            message = adapter.parse_message(case.message_payload)
            self.assertIsNotNone(message, case.provider)
            assert message is not None
            self.assertEqual(message.channel_type, case.provider)
            self.assertEqual(message.channel_thread_id, case.thread_id)
            self.assertEqual(message.channel_user_id, case.user_id)
            self.assertTrue(message.text)
            self.assertIn("channel_context", message.metadata)
            self.assertEqual(message.metadata["channel_context"]["channel_type"], case.provider)

    def test_approval_callbacks_share_the_same_resolution_flow(self) -> None:
        for case in self._provider_cases():
            requested = self.client.post(
                f"/integrations/conversations/channels/{case.provider}/messages",
                json={
                    "channel_thread_id": case.thread_id,
                    "channel_user_id": case.user_id,
                    "channel_display_name": case.display_name,
                    "text": f"Request approval via {case.provider}",
                    "external_message_id": f"{case.provider}-approval-request",
                    "content": {
                        "approval_request": {
                            "approval_type": "workflow_execution",
                            "target_type": "workflow",
                            "target_id": f"workflow-{case.provider}",
                            "summary": f"Approve workflow via {case.provider}",
                        }
                    },
                },
            )
            self.assertEqual(requested.status_code, 200, case.provider)
            approval_id = requested.json()["result"]["approval_request"]["id"]

            callback = self.client.post(
                f"/integrations/conversations/adapters/{case.provider}/webhook",
                json=case.callback_payload(approval_id),
            )
            self.assertEqual(callback.status_code, 200, case.provider)
            body = callback.json()
            self.assertTrue(body["handled"], case.provider)
            self.assertEqual(body["event_type"], "approval_action", case.provider)
            self.assertEqual(body["result"]["approval_request"]["id"], approval_id, case.provider)
            self.assertEqual(body["result"]["approval_request"]["status"], "approved", case.provider)

    def test_outbound_assistant_messages_are_formatted_per_channel(self) -> None:
        target = AdapterInboundMessage(
            channel_type="telegram",
            channel_thread_id="thread-shared-format",
            channel_user_id="user-shared-format",
            channel_display_name="Shared Format User",
            text="",
            external_message_id=None,
            metadata={"channel_context": {"channel_type": "telegram"}, "thread_ts": "1712345678.001"},
        )
        outbound = [{"type": "approval", "approval_request_id": "approval-shared-format", "text": "Approve shared action"}]

        telegram = create_channel_outbound_formatter("telegram").format_messages(outbound, target=target)[0]
        discord = create_channel_outbound_formatter("discord").format_messages(outbound, target=target)[0]
        whatsapp = create_channel_outbound_formatter("whatsapp").format_messages(outbound, target=target)[0]
        slack = create_channel_outbound_formatter("slack").format_messages(outbound, target=target)[0]
        teams = create_channel_outbound_formatter("microsoft-teams").format_messages(outbound, target=target)[0]

        self.assertEqual(telegram["method"], "sendMessage")
        self.assertIn("reply_markup", telegram["payload"])
        self.assertEqual(discord["method"], "createMessage")
        self.assertIn("components", discord["payload"])
        self.assertEqual(whatsapp["method"], "messages")
        self.assertEqual(whatsapp["payload"]["type"], "interactive")
        self.assertEqual(slack["method"], "chat.postMessage")
        self.assertIn("blocks", slack["payload"])
        self.assertEqual(teams["method"], "sendChannelMessage")
        self.assertIn("attachments", teams["payload"])

    def test_duplicate_external_message_ids_replay_idempotently(self) -> None:
        for case in self._provider_cases():
            request_payload = {
                "channel_thread_id": case.thread_id,
                "channel_user_id": case.user_id,
                "channel_display_name": case.display_name,
                "text": f"Hello from {case.provider}",
                "external_message_id": f"{case.provider}-shared-idempotent",
            }

            first = self.client.post(f"/integrations/conversations/channels/{case.provider}/messages", json=request_payload)
            second = self.client.post(f"/integrations/conversations/channels/{case.provider}/messages", json=request_payload)

            self.assertEqual(first.status_code, 200, case.provider)
            self.assertEqual(second.status_code, 200, case.provider)
            self.assertFalse(first.json()["result"].get("idempotent", False), case.provider)
            self.assertTrue(second.json()["result"]["idempotent"], case.provider)
            self.assertEqual(
                second.json()["outbound_messages"][0]["text"],
                first.json()["outbound_messages"][0]["text"],
                case.provider,
            )

    def _provider_cases(self) -> tuple[_ProviderCase, ...]:
        return (
            _ProviderCase(
                provider="telegram",
                thread_id="telegram-thread-shared",
                user_id="telegram-user-shared",
                display_name="Telegram User",
                message_payload={
                    "update_id": 1001,
                    "message": {
                        "message_id": 2001,
                        "chat": {"id": "telegram-thread-shared", "type": "private"},
                        "from": {"id": "telegram-user-shared", "username": "telegram_user"},
                        "text": "Hello from Telegram",
                    },
                },
                callback_payload=lambda approval_id: {
                    "update_id": 1002,
                    "callback_query": {
                        "id": "telegram-callback",
                        "from": {"id": "telegram-user-shared", "username": "telegram_user"},
                        "message": {"chat": {"id": "telegram-thread-shared"}},
                        "data": f"approval:approve:{approval_id}",
                    },
                },
            ),
            _ProviderCase(
                provider="discord",
                thread_id="discord-channel-shared",
                user_id="discord-user-shared",
                display_name="Discord User",
                message_payload={
                    "id": "discord-message-shared",
                    "channel_id": "discord-channel-shared",
                    "guild_id": "discord-guild-shared",
                    "author": {"id": "discord-user-shared", "username": "discord_user"},
                    "content": "Hello from Discord",
                },
                callback_payload=lambda approval_id: {
                    "id": "discord-approval-callback",
                    "type": 3,
                    "channel_id": "discord-channel-shared",
                    "user": {"id": "discord-user-shared", "username": "discord_user"},
                    "data": {"custom_id": f"approval:approve:{approval_id}", "component_type": 2},
                },
            ),
            _ProviderCase(
                provider="whatsapp",
                thread_id="15551230000",
                user_id="15551230000",
                display_name="WhatsApp User",
                message_payload={
                    "entry": [
                        {
                            "changes": [
                                {
                                    "value": {
                                        "metadata": {"phone_number_id": "15551230000"},
                                        "contacts": [
                                            {"wa_id": "15551230000", "profile": {"name": "WhatsApp User"}}
                                        ],
                                        "messages": [
                                            {
                                                "id": "wamid.shared",
                                                "from": "15551230000",
                                                "text": {"body": "Hello from WhatsApp"},
                                            }
                                        ],
                                    }
                                }
                            ]
                        }
                    ]
                },
                callback_payload=lambda approval_id: {
                    "entry": [
                        {
                            "changes": [
                                {
                                    "value": {
                                        "metadata": {"phone_number_id": "15551230000"},
                                        "messages": [
                                            {
                                                "id": "wamid.callback",
                                                "from": "15551230000",
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
            ),
            _ProviderCase(
                provider="slack",
                thread_id="C-slack-shared",
                user_id="U-slack-shared",
                display_name="Slack User",
                message_payload={
                    "type": "event_callback",
                    "team_id": "T-slack-shared",
                    "event_id": "Ev-slack-shared",
                    "event": {
                        "type": "message",
                        "channel": "C-slack-shared",
                        "user": "U-slack-shared",
                        "text": "Hello from Slack",
                        "ts": "1712345678.001",
                    },
                },
                callback_payload=lambda approval_id: {
                    "type": "block_actions",
                    "team": {"id": "T-slack-shared"},
                    "user": {"id": "U-slack-shared", "username": "slack_user"},
                    "container": {
                        "type": "message",
                        "channel_id": "C-slack-shared",
                        "message_ts": "1712345678.001",
                    },
                    "actions": [
                        {
                            "action_id": "approval_approve",
                            "value": f"approval:approve:{approval_id}",
                        }
                    ],
                },
            ),
            _ProviderCase(
                provider="microsoft-teams",
                thread_id="teams-channel-shared",
                user_id="teams-user-shared",
                display_name="Teams User",
                message_payload={
                    "type": "message",
                    "id": "teams-message-shared",
                    "from": {"id": "teams-user-shared", "name": "Teams User"},
                    "conversation": {"id": "teams-channel-shared"},
                    "text": "Hello from Teams",
                    "channelData": {"team": {"id": "teams-team-shared"}, "channel": {"id": "teams-channel-shared"}},
                },
                callback_payload=lambda approval_id: {
                    "type": "invoke",
                    "from": {"id": "teams-user-shared", "name": "Teams User"},
                    "conversation": {"id": "teams-channel-shared"},
                    "value": {
                        "approval": {
                            "approval_action": "approve",
                            "approval_request_id": approval_id,
                        }
                    },
                },
            ),
        )


if __name__ == "__main__":
    unittest.main()
