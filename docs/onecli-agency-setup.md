# OneCLI setup for Agency

Agency embeds OneCLI as a separate-origin credential workspace. Provider secrets are entered in OneCLI, while Agency stores only a verified OneCLI resource reference and non-secret connector metadata.

## Required version and origins

- Run OneCLI v1.41.0 or newer. The compose defaults pin v1.41.0 because Agency relies on the `/v1` metadata API and Telegram URL-path injection.
- Give OneCLI its own origin. A different port is a different origin for local development; production HTTPS should use a dedicated HTTPS origin.
- Set the frontend `NEXT_PUBLIC_ONECLI_APP_URL` to that browser-reachable origin and the backend `ONECLI_API_URL` to the server-reachable OneCLI app URL.

## Configure verification and runtime access

1. Start OneCLI and finish its initial project setup.
2. In OneCLI Settings, create a project API key beginning with `oc_`.
3. Set `ONECLI_CONTROL_API_KEY` to that key and keep `ONECLI_CONTROL_API_KEY_SECRET_REF=env://ONECLI_CONTROL_API_KEY`.
4. Create or map a OneCLI agent token for proxy traffic and configure `ONECLI_AGENT_TOKEN_SECRET_REF` (or the existing owner identity mapping in multi-user deployments).
5. Set `ONECLI_ENABLED=true`, restart Agency, and check the sanitized runtime diagnostics.

The control key is a backend-only credential. It is used for metadata-only `GET /v1/secrets` and `GET /v1/connections` calls. Agency does not request, log, or persist upstream secret values.

## Connector setup lifecycle

1. Agency creates a short-lived setup session and a session-specific resource name.
2. The user completes the prefilled native connection or Generic Secret form inside the isolated OneCLI frame.
3. Agency verifies the resource name, provider/profile, and creation time through OneCLI's metadata API.
4. Agency stores the returned resource ID as an owner-scoped `onecli://` reference and activates the installation.

Browser-supplied credential references and raw runtime secrets are rejected. Unfinished sessions expire after `ONECLI_SETUP_SESSION_TTL_SECONDS` (30 minutes by default), can be resumed before expiry, and can be explicitly abandoned.

## Current compatibility boundary

Self-hosted OneCLI v1.41 exposes usable native flows for providers such as GitHub, Gmail, Google Drive, Jira, Notion, Dropbox, and AWS. Single-token providers use a verified Generic Secret profile. Integrations requiring multiple independent secrets or OAuth refresh orchestration are shown as guide-only until a self-hosted OneCLI resource can represent and refresh them safely.
