from __future__ import annotations

import asyncio
import httpx
import json
import logging
from dataclasses import dataclass
from typing import Any, Literal
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed

from app.api.context import ApiContext
from app.core.config import Settings, get_settings
from app.domain import CredentialDefinition, CredentialStatus
from app.integrations.connectors import normalize_connector_provider_key
from app.integrations.onecli import build_onecli_proxy_url
from app.integrations.secrets import is_onecli_secret_ref, onecli_secret_identifier
from app.services.credentials import CredentialService
from app.services.onecli import OneCLIIdentityMappingService
from .channel_adapters import AdapterInboundMessage, create_chat_channel_adapter
from .channel_delivery import ChannelOutboundDeliveryService
from .channels import ConversationChannelService

logger = logging.getLogger(__name__)

DISCORD_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
DISCORD_GATEWAY_INTENTS = 1 | 512 | 4096 | 32768
DISCORD_PROXY_AUTH_RETRY_MIN_SECONDS = 60.0
DISCORD_PROXY_AUTH_RETRY_MAX_SECONDS = 300.0


@dataclass(slots=True)
class DiscordGatewayListenerConfig:
    credential: CredentialDefinition
    transport: Literal["gateway", "rest_poll"]
    token: str | None
    bot_user_id: str | None
    onecli_identifier: str | None = None
    onecli_agent_token_secret_ref: str | None = None


class DiscordGatewayListenerService:
    def __init__(self, context: ApiContext, settings: Settings | None = None) -> None:
        self.context = context
        self.settings = settings or get_settings()

    async def run_forever(self) -> None:
        while True:
            configs = await self._resolve_listener_configs()
            if not configs:
                logger.debug("Discord listener idle: no resolvable Discord credentials were found.")
                await asyncio.sleep(max(self.settings.discord_gateway_reconnect_delay_seconds, 5.0))
                continue

            tasks = [asyncio.create_task(self._run_listener(config)) for config in configs]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(max(self.settings.discord_gateway_reconnect_delay_seconds, 1.0))

    async def _resolve_listener_configs(self) -> list[DiscordGatewayListenerConfig]:
        credentials = await self.context.credential_repo.list()
        explicit_credential_id = (self.settings.discord_gateway_credential_id or "").strip()
        explicit_token = (self.settings.discord_gateway_bot_token or "").strip()
        explicit_token_claimed = False
        configs: list[DiscordGatewayListenerConfig] = []

        for credential in credentials:
            if normalize_connector_provider_key(credential.provider) != "discord-bot":
                continue
            if credential.status != CredentialStatus.ACTIVE:
                continue
            if explicit_credential_id and credential.id != explicit_credential_id:
                continue

            token_override = explicit_token if explicit_credential_id or not explicit_token_claimed else ""
            listener = await self._resolve_listener_transport(credential, explicit_token=token_override)
            if listener is None:
                continue
            if token_override:
                explicit_token_claimed = True
            configs.append(
                DiscordGatewayListenerConfig(
                    credential=credential,
                    transport=listener["transport"],
                    token=listener.get("token"),
                    bot_user_id=self._credential_bot_user_id(credential),
                    onecli_identifier=listener.get("onecli_identifier"),
                    onecli_agent_token_secret_ref=listener.get("onecli_agent_token_secret_ref"),
                )
            )
        return configs

    async def _resolve_listener_transport(
            self,
            credential: CredentialDefinition,
            *,
            explicit_token: str,
    ) -> dict[str, str] | None:
        if explicit_token:
            return {"transport": "gateway", "token": explicit_token}
        if is_onecli_secret_ref(credential.secret_ref):
            identifier = onecli_secret_identifier(credential.secret_ref)
            if not identifier:
                logger.warning("Skipping Discord listener for credential '%s': empty OneCLI credential ref.",
                               credential.id)
                return None
            agent_token_context = await self._onecli_agent_token_context(credential.owner_user_id)
            agent_token_secret_ref = agent_token_context.get("agent_token_secret_ref")
            if not isinstance(agent_token_secret_ref, str) or not agent_token_secret_ref.strip():
                logger.warning(
                    "Skipping Discord listener for credential '%s': no OneCLI agent token context is available.",
                    credential.id,
                )
                return None
            # Published OneCLI installs can proxy Discord HTTP traffic, but the
            # direct-capable Discord path uses the runtime mirror for Gateway
            # auth. Keep the rest of the listener logic on the mirrored-token
            # path so we do not duplicate raw bot secrets in Agency env.
            return {
                "transport": "rest_poll",
                "onecli_identifier": identifier,
                "onecli_agent_token_secret_ref": agent_token_secret_ref.strip(),
            }
        resolved = await CredentialService(self.context).resolve_credential_secret(credential)
        if resolved.value is None:
            logger.warning(
                "Skipping Discord listener for credential '%s': %s",
                credential.id,
                resolved.error or "secret could not be resolved",
            )
            return None
        return {"transport": "gateway", "token": resolved.value}

    async def _onecli_agent_token_context(self, owner_user_id: str | None) -> dict[str, Any]:
        context = await OneCLIIdentityMappingService(self.context).resolve_agent_token_context(
            owner_user_id=owner_user_id
        )
        if isinstance(context.get("agent_token_secret_ref"), str):
            return context

        settings = self.settings
        if settings.onecli_agent_token_secret_ref:
            return {
                "agent_token_secret_ref": settings.onecli_agent_token_secret_ref,
                "source": "server_configured_agent_token",
                "owner_user_id": owner_user_id,
            }
        return context

    def _credential_bot_user_id(self, credential: CredentialDefinition) -> str | None:
        bot_user_id = credential.metadata.get("bot_user_id")
        if isinstance(bot_user_id, str) and bot_user_id.strip():
            return bot_user_id.strip()
        return None

    async def _run_listener(self, config: DiscordGatewayListenerConfig) -> None:
        proxy_auth_failure_count = 0
        while True:
            retry_delay = max(self.settings.discord_gateway_reconnect_delay_seconds, 1.0)
            try:
                if config.transport == "rest_poll":
                    await self._run_rest_poll_session(config)
                else:
                    await self._run_single_gateway_session(config)
                proxy_auth_failure_count = 0
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as exc:
                proxy_auth_failure_count = 0
                # Discord gateway sessions are long-lived TCP connections; proxies
                # and Discord can drop them without a close frame. Reconnect without
                # logging a traceback for this expected transport churn.
                logger.info(
                    "Discord gateway connection closed for credential '%s'; reconnecting: %s",
                    config.credential.id,
                    exc,
                )
            except httpx.ProxyError as exc:
                if "407" in str(exc):
                    proxy_auth_failure_count += 1
                    retry_delay = min(
                        max(
                            self.settings.discord_gateway_reconnect_delay_seconds,
                            DISCORD_PROXY_AUTH_RETRY_MIN_SECONDS,
                        ) * (2 ** min(proxy_auth_failure_count - 1, 3)),
                        DISCORD_PROXY_AUTH_RETRY_MAX_SECONDS,
                    )
                    if proxy_auth_failure_count == 1:
                        logger.error(
                            "Discord listener proxy authentication failed for credential '%s'. "
                            "Verify or rotate the owner-scoped OneCLI agent token mapping; "
                            "retrying with bounded backoff.",
                            config.credential.id,
                        )
                    else:
                        # The first error contains the operator action. Keep later
                        # retries at debug level so a static credential fault cannot
                        # flood production logs every few seconds.
                        logger.debug(
                            "Discord listener proxy authentication is still failing for credential '%s'; "
                            "retrying in %.1f seconds.",
                            config.credential.id,
                            retry_delay,
                        )
                else:
                    proxy_auth_failure_count = 0
                    logger.exception(
                        "Discord listener proxy failed for credential '%s'; reconnecting.",
                        config.credential.id,
                    )
            except Exception:
                proxy_auth_failure_count = 0
                logger.exception(
                    "Discord listener failed for credential '%s'; reconnecting.",
                    config.credential.id,
                )
            await asyncio.sleep(retry_delay)

    async def _run_single_gateway_session(self, config: DiscordGatewayListenerConfig) -> None:
        sequence: int | None = None
        bot_user_id = config.bot_user_id
        async with websocket_connect(DISCORD_GATEWAY_URL, max_size=2_000_000) as websocket:
            hello = json.loads(await websocket.recv())
            heartbeat_interval_ms = int((hello.get("d") or {}).get("heartbeat_interval") or 45000)
            heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(websocket, heartbeat_interval_ms / 1000.0, lambda: sequence)
            )
            try:
                await websocket.send(
                    json.dumps(
                        {
                            "op": 2,
                            "d": {
                                "token": config.token,
                                "intents": DISCORD_GATEWAY_INTENTS,
                                "properties": {
                                    "os": "macos",
                                    "browser": "agency",
                                    "device": "agency",
                                },
                            },
                        }
                    )
                )
                async for raw_message in websocket:
                    payload = json.loads(raw_message)
                    if isinstance(payload.get("s"), int):
                        sequence = int(payload["s"])
                    op = payload.get("op")
                    event_type = payload.get("t")
                    data = payload.get("d") if isinstance(payload.get("d"), dict) else {}
                    if op == 7:
                        return
                    if op == 0 and event_type == "READY":
                        ready_user = data.get("user") if isinstance(data.get("user"), dict) else {}
                        ready_user_id = ready_user.get("id")
                        if isinstance(ready_user_id, str) and ready_user_id.strip():
                            bot_user_id = ready_user_id.strip()
                        continue
                    if op == 0 and event_type == "MESSAGE_CREATE":
                        await self._handle_message_event(
                            config=config,
                            bot_user_id=bot_user_id,
                            payload=data,
                        )
            finally:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def _run_rest_poll_session(self, config: DiscordGatewayListenerConfig) -> None:
        known_cursors: dict[str, int] = {}
        bootstrapped = False
        async with httpx.AsyncClient(
                headers=self._rest_headers(config),
                timeout=10.0,
                **self._rest_request_kwargs(config),
        ) as client:
            while True:
                bot_user_id = config.bot_user_id or await self._fetch_bot_user_id(client)
                if bot_user_id and not config.bot_user_id:
                    config.bot_user_id = bot_user_id

                channel_ids = await self._list_poll_channels(client, config)
                for channel_id in channel_ids:
                    messages = await self._fetch_channel_messages(client, channel_id)
                    if not messages:
                        continue
                    current_max = max(self._snowflake_int(str(item.get("id") or "0")) for item in messages)
                    previous_max = known_cursors.get(channel_id)
                    if previous_max is None and not bootstrapped:
                        known_cursors[channel_id] = current_max
                        continue
                    new_messages = [
                        item for item in messages
                        if self._snowflake_int(str(item.get("id") or "0")) > (previous_max or 0)
                    ]
                    for item in sorted(new_messages,
                                       key=lambda message: self._snowflake_int(str(message.get("id") or "0"))):
                        await self._handle_message_event(
                            config=config,
                            bot_user_id=bot_user_id,
                            payload=item,
                        )
                    known_cursors[channel_id] = max(previous_max or 0, current_max)
                bootstrapped = True
                await asyncio.sleep(max(self.settings.discord_gateway_reconnect_delay_seconds, 2.0))

    def _rest_headers(self, config: DiscordGatewayListenerConfig) -> dict[str, str] | None:
        if config.transport == "rest_poll" and config.onecli_identifier:
            return None
        if config.token:
            return {"Authorization": f"Bot {config.token}"}
        return None

    def _rest_request_kwargs(self, config: DiscordGatewayListenerConfig) -> dict[str, Any]:
        if config.transport != "rest_poll" or not config.onecli_identifier:
            return {}
        kwargs: dict[str, Any] = {
            "proxy": build_onecli_proxy_url(
                self.settings.onecli_gateway_url,
                config.onecli_agent_token_secret_ref,
            )
        }
        if self.settings.onecli_gateway_ca_bundle_path:
            kwargs["verify"] = self.settings.onecli_gateway_ca_bundle_path
        return kwargs

    async def _fetch_bot_user_id(self, client: httpx.AsyncClient) -> str | None:
        response = await client.get("https://discord.com/api/v10/users/@me")
        response.raise_for_status()
        payload = response.json()
        bot_user_id = payload.get("id")
        if isinstance(bot_user_id, str) and bot_user_id.strip():
            return bot_user_id.strip()
        return None

    async def _list_poll_channels(
            self,
            client: httpx.AsyncClient,
            config: DiscordGatewayListenerConfig,
    ) -> list[str]:
        channel_ids: set[str] = set()
        default_guild_id = str(config.credential.metadata.get("default_guild_id") or "").strip()
        if default_guild_id:
            response = await client.get(f"https://discord.com/api/v10/guilds/{default_guild_id}/channels")
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, list):
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    channel_type = item.get("type")
                    channel_id = str(item.get("id") or "").strip()
                    if channel_type in {0, 5, 11, 12} and channel_id:
                        channel_ids.add(channel_id)

        response = await client.get("https://discord.com/api/v10/users/@me/channels")
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                channel_id = str(item.get("id") or "").strip()
                if item.get("type") == 1 and channel_id:
                    channel_ids.add(channel_id)
        return sorted(channel_ids)

    async def _fetch_channel_messages(self, client: httpx.AsyncClient, channel_id: str) -> list[dict[str, Any]]:
        response = await client.get(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            params={"limit": 25},
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    def _snowflake_int(self, value: str) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    async def _heartbeat_loop(self, websocket: Any, interval_seconds: float, sequence_provider: Any) -> None:
        while True:
            await asyncio.sleep(max(interval_seconds, 1.0))
            await websocket.send(json.dumps({"op": 1, "d": sequence_provider()}))

    async def _handle_message_event(
            self,
            *,
            config: DiscordGatewayListenerConfig,
            bot_user_id: str | None,
            payload: dict[str, Any],
    ) -> None:
        author = payload.get("author") if isinstance(payload.get("author"), dict) else {}
        if author.get("bot") is True:
            return
        if not self._should_process_message(payload, bot_user_id=bot_user_id):
            return

        normalized_payload = dict(payload)
        normalized_payload["content"] = self._normalized_message_content(payload, bot_user_id=bot_user_id)
        adapter = create_chat_channel_adapter(self.context, "discord")
        message = adapter.parse_message(normalized_payload)
        if message is None:
            return

        enriched_metadata = {
            **message.metadata,
            "adapter": "discord",
            "discord_gateway_event": True,
            "discord_event_type": "MESSAGE_CREATE",
        }
        inbound_message = AdapterInboundMessage(
            channel_type=message.channel_type,
            channel_thread_id=message.channel_thread_id,
            channel_user_id=message.channel_user_id,
            channel_display_name=message.channel_display_name,
            text=message.text,
            external_message_id=message.external_message_id,
            metadata=enriched_metadata,
        )

        result = await ConversationChannelService(self.context).handle_inbound_message(
            channel_type=inbound_message.channel_type,
            channel_thread_id=inbound_message.channel_thread_id,
            channel_user_id=inbound_message.channel_user_id,
            channel_display_name=inbound_message.channel_display_name,
            internal_user_id=None,
            text=inbound_message.text,
            response_mode="sync",
            message_id=inbound_message.external_message_id,
            content=None,
            metadata=inbound_message.metadata,
        )
        provider_outbound_messages = adapter.format_outbound_messages(
            result.get("outbound_messages", []),
            target=inbound_message,
        )
        if not provider_outbound_messages:
            return
        if not config.credential.owner_user_id:
            logger.warning(
                "Discord gateway listener cannot deliver replies for credential '%s' because owner_user_id is missing.",
                config.credential.id,
            )
            return
        delivery_service = ChannelOutboundDeliveryService(self.context)
        await delivery_service.deliver_for_owner(
            provider="discord",
            credential_id=config.credential.id,
            owner_user_id=config.credential.owner_user_id,
            provider_outbound_messages=provider_outbound_messages,
        )

    def _should_process_message(self, payload: dict[str, Any], *, bot_user_id: str | None) -> bool:
        if not self.settings.discord_gateway_mention_only:
            return True
        if payload.get("guild_id") in (None, ""):
            return True
        if not bot_user_id:
            return False
        mentions = payload.get("mentions") if isinstance(payload.get("mentions"), list) else []
        for mention in mentions:
            if isinstance(mention, dict) and str(mention.get("id") or "").strip() == bot_user_id:
                return True
        content = str(payload.get("content") or "")
        mention_tokens = (f"<@{bot_user_id}>", f"<@!{bot_user_id}>")
        return any(token in content for token in mention_tokens)

    def _normalized_message_content(self, payload: dict[str, Any], *, bot_user_id: str | None) -> str:
        content = str(payload.get("content") or "").strip()
        if not content or not bot_user_id or not self.settings.discord_gateway_mention_only:
            return content
        for token in (f"<@{bot_user_id}>", f"<@!{bot_user_id}>"):
            content = content.replace(token, " ")
        return " ".join(content.split())


__all__ = ["DiscordGatewayListenerService", "DiscordGatewayListenerConfig"]
