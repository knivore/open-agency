from __future__ import annotations

import asyncio
import httpx
import os
import unittest
from unittest.mock import AsyncMock, patch
from websockets.exceptions import ConnectionClosedError

from app.api.context import create_test_api_context
from app.core.config import get_settings, reset_settings_cache
from app.domain import CredentialDefinition, OneCLIIdentityMapping
from app.services.conversations.discord_gateway import DiscordGatewayListenerConfig, DiscordGatewayListenerService


class DiscordGatewayListenerServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_settings_cache()
        self.context = create_test_api_context()

    def tearDown(self) -> None:
        reset_settings_cache()

    def test_resolve_listener_configs_uses_direct_env_secret(self) -> None:
        asyncio.run(
            self.context.credential_repo.save(
                CredentialDefinition(
                    id="cred-discord-env",
                    owner_user_id="dev-user",
                    name="Discord Env",
                    provider="discord-bot",
                    secret_ref="env://TEST_DISCORD_GATEWAY_TOKEN",
                    metadata={"bot_user_id": "bot-123"},
                )
            )
        )
        with patch.dict(
            os.environ,
            {
                "TEST_DISCORD_GATEWAY_TOKEN": "discord-token",
                "DISCORD_GATEWAY_LISTENER_ENABLED": "true",
            },
            clear=False,
        ):
            reset_settings_cache()
            service = DiscordGatewayListenerService(self.context, settings=get_settings())
            configs = asyncio.run(service._resolve_listener_configs())  # noqa: SLF001

        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0].credential.id, "cred-discord-env")
        self.assertEqual(configs[0].token, "discord-token")
        self.assertEqual(configs[0].bot_user_id, "bot-123")

    def test_resolve_listener_configs_uses_onecli_rest_polling_without_bot_token_override(self) -> None:
        asyncio.run(
            self.context.credential_repo.save(
                CredentialDefinition(
                    id="cred-discord-onecli",
                    owner_user_id="dev-user",
                    name="Discord OneCLI",
                    provider="discord-bot",
                    secret_ref="onecli://users/dev-user/discord-bot/cred-discord-onecli",
                    metadata={"bot_user_id": "bot-123"},
                )
            )
        )
        asyncio.run(
            self.context.onecli_identity_mapping_repo.save(
                OneCLIIdentityMapping(
                    id="mapping-onecli-discord",
                    owner_user_id="dev-user",
                    name="Local OneCLI",
                    onecli_agent_id="agent-onecli-discord",
                    agent_token_secret_ref="env://ONECLI_AGENT_TOKEN",
                )
            )
        )
        with (
            patch.dict(os.environ, {"DISCORD_GATEWAY_LISTENER_ENABLED": "true"}, clear=False),
        ):
            reset_settings_cache()
            service = DiscordGatewayListenerService(self.context, settings=get_settings())
            configs = asyncio.run(service._resolve_listener_configs())  # noqa: SLF001

        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0].transport, "rest_poll")
        self.assertIsNone(configs[0].token)
        self.assertEqual(configs[0].onecli_identifier, "users/dev-user/discord-bot/cred-discord-onecli")
        self.assertEqual(configs[0].onecli_agent_token_secret_ref, "env://ONECLI_AGENT_TOKEN")

    def test_mention_only_processing_strips_bot_mention(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DISCORD_GATEWAY_LISTENER_ENABLED": "true",
                "DISCORD_GATEWAY_MENTION_ONLY": "true",
            },
            clear=False,
        ):
            reset_settings_cache()
            service = DiscordGatewayListenerService(self.context, settings=get_settings())
            payload = {
                "guild_id": "guild-1",
                "content": "<@12345> review this workflow",
                "mentions": [{"id": "12345"}],
            }

            self.assertTrue(service._should_process_message(payload, bot_user_id="12345"))  # noqa: SLF001
            self.assertEqual(
                service._normalized_message_content(payload, bot_user_id="12345"),  # noqa: SLF001
                "review this workflow",
            )

    def test_gateway_disconnect_reconnects_without_exception_log(self) -> None:
        async def exercise() -> None:
            credential = CredentialDefinition(
                id="cred-discord-drop",
                owner_user_id="dev-user",
                name="Discord Drop",
                provider="discord-bot",
                secret_ref="env://TEST_DISCORD_GATEWAY_TOKEN",
            )
            config = DiscordGatewayListenerConfig(
                credential=credential,
                transport="gateway",
                token="discord-token",
                bot_user_id="bot-123",
            )
            service = DiscordGatewayListenerService(self.context, settings=get_settings())

            with (
                patch.object(
                    service,
                    "_run_single_gateway_session",
                    AsyncMock(side_effect=ConnectionClosedError(None, None)),
                ),
                patch("app.services.conversations.discord_gateway.logger.exception") as log_exception,
                patch("app.services.conversations.discord_gateway.logger.info") as log_info,
                patch(
                    "app.services.conversations.discord_gateway.asyncio.sleep",
                    AsyncMock(side_effect=asyncio.CancelledError),
                ),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await service._run_listener(config)  # noqa: SLF001

            log_exception.assert_not_called()
            log_info.assert_called_once()

        asyncio.run(exercise())

    def test_proxy_auth_failure_logs_actionable_error_without_traceback(self) -> None:
        async def exercise() -> None:
            credential = CredentialDefinition(
                id="cred-discord-onecli-auth",
                owner_user_id="dev-user",
                name="Discord OneCLI",
                provider="discord-bot",
                secret_ref="onecli://users/dev-user/discord-bot/cred-discord-onecli-auth",
            )
            config = DiscordGatewayListenerConfig(
                credential=credential,
                transport="rest_poll",
                token=None,
                bot_user_id=None,
                onecli_identifier="users/dev-user/discord-bot/cred-discord-onecli-auth",
                onecli_agent_token_secret_ref="env://ONECLI_AGENT_TOKEN",
            )
            service = DiscordGatewayListenerService(self.context, settings=get_settings())

            with (
                patch.object(
                    service,
                    "_run_rest_poll_session",
                    AsyncMock(
                        side_effect=[
                            *[httpx.ProxyError("407 Proxy Authentication Required") for _ in range(5)],
                            asyncio.CancelledError(),
                        ]
                    ),
                ),
                patch("app.services.conversations.discord_gateway.logger.error") as log_error,
                patch("app.services.conversations.discord_gateway.logger.debug") as log_debug,
                patch("app.services.conversations.discord_gateway.logger.exception") as log_exception,
                patch(
                    "app.services.conversations.discord_gateway.asyncio.sleep",
                    AsyncMock(),
                ) as sleep,
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await service._run_listener(config)  # noqa: SLF001

            log_error.assert_called_once()
            self.assertIn("Verify or rotate", log_error.call_args.args[0])
            self.assertEqual(log_debug.call_count, 4)
            log_exception.assert_not_called()
            self.assertEqual(
                [item.args[0] for item in sleep.await_args_list],
                [60.0, 120.0, 240.0, 300.0, 300.0],
            )

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
