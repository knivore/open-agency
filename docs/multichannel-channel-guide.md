# Multichannel Channel Guide

This guide explains how to add a new backend chat channel without changing the main-agent logic.

The rule is simple:

- the main agent owns behavior
- the conversation engine owns approvals and tool routing
- the channel adapter owns transport-specific parsing, formatting, and verification

## Contract

A new chat channel should add exactly these pieces:

1. One adapter that parses inbound payloads into the shared conversation shape.
2. One outbound formatter that turns assistant messages into provider payloads.
3. One webhook verifier that checks the provider's request signature or secret.
4. One connector definition that documents the credential and identity metadata.
5. One set of tests that prove the channel works without changing assistant behavior.

The shared protocol surface is defined in:

- [`app/services/conversations/channel_contract.py`](../app/services/conversations/channel_contract.py)
- [`app/services/conversations/channel_registry.py`](../app/services/conversations/channel_registry.py)

## Add A New Channel

Use the following sequence when adding a provider:

1. Add the provider to the channel registry.
2. Add the provider to `ConversationChannelType` if it should be persisted as a first-class chat channel.
3. Add connector metadata requirements and setup-guide fields.
4. Implement the inbound adapter.
5. Implement the outbound formatter.
6. Implement the webhook verifier.
7. Register the adapter and formatter in the factory helpers.
8. Add delivery support in the transport layer if the provider needs a custom API shape.
9. Add tests for parsing, verification, delivery, and approval callbacks.
10. Update the checklist when the new provider is genuinely supported.

## Microsoft Teams Example

Teams is the current example channel for this contract. The implementation is intentionally small so future channels can copy the same pattern.

Relevant code paths:

- [`app/domain/conversations.py`](../app/domain/conversations.py)
- [`app/services/conversations/channel_registry.py`](../app/services/conversations/channel_registry.py)
- [`app/services/conversations/channel_webhooks.py`](../app/services/conversations/channel_webhooks.py)
- [`app/services/conversations/channel_adapters.py`](../app/services/conversations/channel_adapters.py)
- [`app/services/conversations/channel_delivery.py`](../app/services/conversations/channel_delivery.py)
- [`app/integrations/connectors.py`](../app/integrations/connectors.py)
- [`app/services/integrations_registry.py`](../app/services/integrations_registry.py)
- [`tests/conversations/test_channels_api.py`](../tests/conversations/test_channels_api.py)

Teams currently demonstrates:

- registry normalization from `teams` to `microsoft-teams`
- inbound message parsing into the shared adapter contract
- outbound formatting through `sendChannelMessage`
- approval prompts rendered as adaptive cards with submit actions
- webhook verification using a dedicated secret hook
- connector metadata for tenant, team, channel, and webhook secret setup

## What Should Not Change

When adding a new channel, avoid duplicating assistant logic in the adapter layer. The adapter should not:

- decide workflow policy
- decide approval policy
- create personas
- create tools
- rewrite the main-agent prompt

Those behaviors belong in the conversation engine and the main-agent workflow.

## When To Use A New Adapter

Add a new adapter when the channel has any of these differences:

- a different webhook signing scheme
- a different message event schema
- a different approval callback schema
- a different outbound message API
- a different threading model

If the provider already fits one of the existing adapter shapes, prefer extending the registry and formatter instead of introducing a new branch in the conversation engine.

## Discord Limitation

With the published OneCLI image, Discord ordinary chat for installed integrations is limited to server/channel polling.

- interaction webhooks still cover slash commands and buttons
- outbound delivery still works through the saved credential
- server/channel chat works through the background listener
- Discord DMs do not work in the current listener path

Document this limitation clearly in operator and setup guides instead of implying full Discord Gateway parity.

## Telegram Notes

Telegram setup is webhook-first, not Gateway-first.

- the bot token comes from BotFather
- the numeric `bot_user_id` comes from `getMe`
- the webhook URL must include `credential_id=<installation_id>` so Agency can reply through the same installation
- the webhook can be auto-registered when the launcher publishes `AGENCY_PUBLIC_WEBHOOK_BASE_URL`

Do not treat the Telegram display name or username as the `bot_user_id`; only the numeric `result.id` from `getMe`
belongs in that field.
