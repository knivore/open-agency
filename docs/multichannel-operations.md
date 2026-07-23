# Multichannel Operations

This guide captures the operator-facing setup details for the supported chat channels.

The backend treats each channel as a transport adapter over the same main-agent conversation engine.

OneCLI compatibility follows the provider auth shape:

- `direct-only`: Telegram, because the bot token is embedded in the URL path and OneCLI cannot inject it through the proxy.
- `direct-capable`: Discord, because the same runtime mirror can support Gateway auth and DMs.
- `proxy-compatible`: WhatsApp, Slack, Microsoft Teams, and Twilio, because their credentials are header-based
  or otherwise safe to inject at request time.

Each connector installation gets its own OneCLI ref, so a single Agency user can keep multiple Discord bots or Telegram
bots side by side without sharing a `/default` credential path. For direct transport, Agency mirrors the same secret
into a runtime secret record at completion time so the provider call stays direct while OneCLI remains the setup
surface. Direct delivery and health checks also bypass inherited proxy env vars so Telegram and direct-capable Discord
stay off the OneCLI transport path. When the launcher publishes `AGENCY_PUBLIC_WEBHOOK_BASE_URL`, Telegram completion can also register the
webhook automatically against the live public URL, which removes the manual `setWebhook` step for the normal launcher
flow. Agency also persists that public base URL and re-registers active Telegram webhooks on backend startup, so a
restart can keep the bot pointed at the current Cloudflare or ngrok URL without another manual step.

## Supported Providers

- Discord
- Telegram
- WhatsApp
- Slack
- Microsoft Teams

## Environment Variables

### Discord

- `DISCORD_BOT_TOKEN`
- `DISCORD_WEBHOOK_PUBLIC_KEY`

`DISCORD_WEBHOOK_PUBLIC_KEY` should be the Discord application Public Key value from the Discord Developer Portal. It is
not the Discord interaction endpoint URL and not a Discord webhook URL.

### Telegram

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`

Telegram webhook registration requires a `secret_token` so Telegram can verify the callback. Agency generates and stores
that secret token during connector completion when you do not provide one, so you usually do not need to create a
separate Telegram secret manually. If you register the webhook yourself, use the stored `webhook_secret_token` from the
Telegram installation metadata.

Telegram metadata should include the numeric `bot_user_id` from `getMe` and the bot `username` without `@`.
To find the bot user id:

```bash
curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" | jq
```

Use the `result.id` value as `bot_user_id`.

## Telegram Setup

1. Create the bot in @BotFather and copy the Bot API token.
2. Run `getMe` with that token and copy the numeric `result.id` as `bot_user_id`.
3. Store the token in the Telegram connector credential and save `bot_user_id` plus `bot_username` as metadata.
4. Configure the webhook URL to:
   - `/integrations/conversations/adapters/telegram/webhook?credential_id=<installation_id>`
   - If you are registering the webhook manually, include the stored `webhook_secret_token` as Telegram's
     `secret_token`.
5. Send a direct message from an allowed Telegram account and confirm the assistant replies in the Telegram app.

### WhatsApp

- `WHATSAPP_ACCESS_TOKEN`
- `WHATSAPP_APP_SECRET`
- `WHATSAPP_PHONE_NUMBER_ID`
- `WHATSAPP_API_VERSION` if you need a version override

### Slack

- `SLACK_BOT_TOKEN`
- `SLACK_SIGNING_SECRET`

### Microsoft Teams

- `TEAMS_BOT_TOKEN`
- `TEAMS_WEBHOOK_SECRET`

## Required Connector Metadata

### Discord

- `webhook_public_key`
- `application_id`
- `bot_user_id`
- `default_guild_id` when you want a default delivery target

`webhook_public_key` must be the Discord application Public Key hex string. Do not paste a Discord webhook URL into
that field.

## Discord Setup

Use this sequence for a working Discord transport:

1. Create or open the Discord application in the Discord Developer Portal.
2. Add a bot to the application.
3. Copy these values from Discord:
   - Bot Token
   - Application ID
   - Public Key
   - Guild ID for the first server you will test in
4. Invite the bot with `bot` and `applications.commands` scopes.
5. Store the bot token as the Agency credential secret.
6. Store `application_id`, `bot_user_id`, `default_guild_id`, and `webhook_public_key` as Agency credential metadata.
7. Set the Discord interactions endpoint URL to the public backend URL ending in:
   - `/integrations/conversations/adapters/discord/webhook`
   - For local development, expose the backend with either Cloudflare Tunnel or ngrok and paste the full public URL
     ending in that path into Discord.
8. Create a trusted channel identity mapping for your Discord user id if you want approvals, workflow edits, tool edits,
   or other protected mutations to resolve as your internal Agency user.
9. Run the backend smoke test:

```bash
./.venv/bin/python -m app.cli smoke-test-discord --owner-user-id YOUR_USER_ID --discord-user-id YOUR_DISCORD_USER_ID
```

10. Send a real Discord message and confirm:
    - a conversation is created or reused
    - the assistant replies in Discord
    - approval buttons work when a protected action is requested

For ordinary Discord channel chat, Agency starts a background Discord listener in addition to the interactions webhook:

- Discord is direct-capable, so the installed integration can work without a raw bot token in backend env while still
  supporting REST-polled channel chat through OneCLI and Gateway-style chat through the runtime mirror.
- Direct or `env://` Discord credentials can still use the optional local overrides below if you want websocket-style
  Gateway handling:

```env
DISCORD_GATEWAY_LISTENER_ENABLED=true
DISCORD_GATEWAY_BOT_TOKEN=PASTE_THE_RAW_BOT_TOKEN
DISCORD_GATEWAY_CREDENTIAL_ID=YOUR_DISCORD_CREDENTIAL_ID
DISCORD_GATEWAY_MENTION_ONLY=true
DISCORD_GATEWAY_RECONNECT_DELAY_SECONDS=5
```

Notes:

- The interactions webhook alone only covers slash commands, buttons, and similar callbacks.
- Plain channel messages still need the background listener path.
- Direct-capable installs satisfy that listener path through the mirrored runtime secret and Gateway auth.
- Discord DMs are supported when the credential is completed in direct mode. `DISCORD_GATEWAY_BOT_TOKEN` remains only
  as a direct-credential fallback if you need true Gateway behavior locally.

Common mistakes:

- putting a Discord webhook URL into `webhook_public_key`
- confusing the Public Key with the Bot Token
- using the backend webhook route as the Public Key value
- enabling the interactions webhook and expecting plain channel messages to arrive without the Gateway listener
- assuming Discord DMs are supported with the current listener path
- skipping the trusted identity mapping and expecting protected mutations to work

### Telegram

- `webhook_secret_ref` or `webhook_secret_token`
- `bot_user_id`
- `bot_username`

If you do not supply a webhook secret token during setup, Agency generates one during completion and stores it in the
connector metadata so the Telegram webhook can still be verified automatically.

### WhatsApp

- `phone_number_id`
- `app_secret_ref` or `app_secret`
- `business_account_id`
- `display_phone_number`

### Slack

- `signing_secret_ref` or `signing_secret`
- `workspace_id`
- `workspace_name`
- `bot_user_id`
- `default_channel_id` if you want a fallback delivery target

### Microsoft Teams

- `webhook_secret_ref` or `webhook_secret`
- `tenant_id`
- `team_id`
- `channel_id`

## Local Webhook Testing

For local development, point provider webhooks at the backend adapter routes:

- `/integrations/conversations/adapters/discord/webhook`
- `/integrations/conversations/adapters/telegram/webhook?credential_id=<installation_id>`
- `/integrations/conversations/adapters/whatsapp/webhook`
- `/integrations/conversations/adapters/slack/webhook`
- `/integrations/conversations/adapters/microsoft-teams/webhook`

For Telegram, include the installation id as `credential_id` so the webhook handler can send replies back through the same connector installation.
The launcher prints the public tunnel endpoint for each provider after startup. For example, a local ngrok or Cloudflare run
will emit a Discord endpoint like `https://<public-host>/integrations/conversations/adapters/discord/webhook`,
Telegram endpoint like `https://<public-host>/integrations/conversations/adapters/telegram/webhook`, and WhatsApp endpoint
like `https://<public-host>/integrations/conversations/adapters/whatsapp/webhook`.

When you configure Telegram for a direct-capable installation, use the credential-scoped form:

- `https://<public-host>/integrations/conversations/adapters/telegram/webhook?credential_id=<installation_id>`

This keeps multiple Telegram installations distinct while still letting the webhook handler deliver replies through the
same installation record.

The launcher now exports `AGENCY_PUBLIC_WEBHOOK_BASE_URL` before backend startup so Telegram completion can call
`setWebhook` automatically. It also stores the current public webhook base URL in the backend database and re-applies it
to active Telegram installations during startup reconciliation. If you bypass the launcher, set that env var yourself
or record a public endpoint with `python -m app.cli public-endpoint record --url https://...`.

Discord note:

- Agency can show you the exact current Discord interactions endpoint URL, but Discord still requires you to paste that
  URL into the Developer Portal manually.

Use one public tunnel for the backend as a whole, not one tunnel per provider.

- Prefer Cloudflare Tunnel when the laptop or network already runs through Cloudflare Zero Trust.
- Prefer ngrok when you do not have TLS interception and just need a quick local public URL.
- In cloud or staging deployments, disable both local tunnel options and point providers at the deployed backend host.

Launcher examples:

```bash
AGENCY_PUBLIC_TUNNEL_PROVIDER=cloudflare ./agency start
AGENCY_PUBLIC_TUNNEL_PROVIDER=ngrok ./agency start
AGENCY_PUBLIC_TUNNEL_PROVIDER=none ./agency start
```

Cloudflare local modes:

- Quick tunnel: no extra Cloudflare account wiring in the launcher; `cloudflared` prints a temporary
  `https://<name>.trycloudflare.com` URL.
- Managed tunnel: set both `AGENCY_CLOUDFLARE_TUNNEL_TOKEN` and `AGENCY_CLOUDFLARE_TUNNEL_PUBLIC_URL` before startup so
  the launcher can run `cloudflared tunnel run --token ...` and print the stable public URL.

Recommended local validation loop:

1. Start the backend.
2. Set the provider secret or token in the environment.
3. Create or update the connector credential with the provider metadata.
4. Send a real event payload into the webhook route.
5. Confirm the response includes `handled=true` and the outbound provider messages.

## Production Webhook Setup

Use a provider-specific public webhook endpoint that reaches the same adapter routes in production.

The adapter should verify signatures or shared secrets before payload parsing:

- Discord: Ed25519 request signature
- Telegram: bot secret token
- WhatsApp: HMAC signature
- Slack: signing secret and timestamp
- Microsoft Teams: Teams webhook secret

## Identity Mapping

External channel identities should map to internal Agency users before you allow trusted mutations.

Use the channel identity mapping API:

- `POST /integrations/conversations/channel-identity-mappings`
- `GET /integrations/conversations/channel-identity-mappings`

Use trusted mappings for:

- approvals
- workflow mutations
- agent edits
- tool mutations
- other protected actions

## Approval Testing

End-to-end approval validation should check:

1. a provider message creates an approval request
2. the provider callback resolves the same approval request
3. the approval status is visible in the conversation result
4. the outbound provider payload includes the correct action controls

For Teams, approval prompts should use adaptive card actions. For the other chat providers, use the existing provider-native interactive payloads.
