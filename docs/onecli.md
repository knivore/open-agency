# OneCLI Credential Gateway

OneCLI is the credential gateway for Agency external-service calls. It lets Agency keep workflow policy,
approvals, execution state, and audit events while moving external-service credential storage and injection out of
agent-visible runtime paths.

Use this runbook for local bring-up, runtime enforcement checks, operator investigation, and connector-delivery
migration notes.

## Local Services

The local Compose stack includes opt-in OneCLI services behind the `onecli` profile:

- `onecli`: OneCLI dashboard and gateway, using `ghcr.io/onecli/onecli`
- `onecli-postgres`: OneCLI-owned Postgres database
- `onecli_app_data`: persistent OneCLI app data
- `onecli_postgres_data`: persistent OneCLI database data

Ports are bound to localhost by default:

- dashboard: `http://127.0.0.1:10254`
- gateway: `http://127.0.0.1:10255`

Start OneCLI:

```bash
docker compose --profile onecli up -d onecli
```

Check status:

```bash
docker compose --profile onecli ps onecli onecli-postgres
docker compose --profile onecli logs -f onecli
```

Stop OneCLI without deleting data:

```bash
docker compose --profile onecli stop onecli onecli-postgres
```

Delete OneCLI local data only when intentionally resetting the vault:

```bash
docker compose --profile onecli down
docker volume rm agency_onecli_app_data agency_onecli_postgres_data
```

## Local Configuration

Copy `.env.example` to `.env` and review these values:

```env
ONECLI_VERSION=latest
ONECLI_IMAGE=ghcr.io/onecli/onecli:latest
ONECLI_POSTGRES_IMAGE=docker.io/postgres:17
ONECLI_BIND_HOST=127.0.0.1
ONECLI_APP_PORT=10254
ONECLI_GATEWAY_PORT=10255
ONECLI_APP_URL=http://127.0.0.1:10254
ONECLI_POSTGRES_USER=onecli
ONECLI_POSTGRES_PASSWORD=change-me-onecli-postgres-password
ONECLI_POSTGRES_DB=onecli
ONECLI_NEXTAUTH_SECRET=
ONECLI_SECRET_ENCRYPTION_KEY=
ONECLI_ENABLED=false
ONECLI_API_URL=http://localhost:10254
ONECLI_GATEWAY_URL=http://localhost:10255
ONECLI_GATEWAY_CA_BUNDLE_PATH=
ONECLI_GATEWAY_CA_BUNDLE_CONTAINER_PATH=/etc/agency/onecli/ca.pem
ONECLI_AGENT_TOKEN_SECRET_REF=
ONECLI_FORCE_FOR_HTTP_TOOLS=false
ONECLI_FORCE_FOR_ISOLATED_WORKERS=false
ONECLI_ALLOW_GLOBAL_AGENT_TOKEN_FALLBACK=false
ONECLI_MULTI_USER_MODE=false
ONECLI_EXTERNAL_CALLS_DISABLED=false
ONECLI_WORKER_EGRESS_MODE=proxy_env_only
ONECLI_WORKER_EGRESS_NETWORK=agency_onecli_worker_egress
ONECLI_NODE_PROXY_BOOTSTRAP_PATH=/app/app/runtime/node_onecli_proxy.cjs
ONECLI_WORKER_NO_PROXY=localhost,127.0.0.1,::1,postgres,redis,backend,agency-backend,onecli,onecli-postgres,host.docker.internal
```

For local development, binding to `127.0.0.1` keeps the dashboard and gateway off the LAN. For team or remote access,
put OneCLI behind an authenticated private route or reverse proxy and set `ONECLI_APP_URL` to the browser-visible URL
before configuring OAuth credentials.

When local Agency startup manages OneCLI through `./agency start` or `./run.sh start`, the launcher copies the gateway
CA from the OneCLI container into
`certs/onecli-gateway-ca.pem`
and writes that absolute path into `.env` as `ONECLI_GATEWAY_CA_BUNDLE_PATH`. Host-side CLI helpers, connector smoke
tests, and other local Agency processes need that CA bundle to trust the OneCLI MITM gateway.

When direct-capable connectors mirror secrets into Agency runtime storage, production deployments should set
`AGENCY_RUNTIME_SECRET_KEY` to a valid Fernet key so those runtime secrets can be encrypted at rest.

Before multi-user access:

- set strong `ONECLI_POSTGRES_PASSWORD`, `ONECLI_NEXTAUTH_SECRET`, and `ONECLI_SECRET_ENCRYPTION_KEY`
- decide the authentication mode for the OneCLI dashboard
- configure backups for both OneCLI volumes
- expose the dashboard only through a private/authenticated route

## Agency Identity Mappings

Agency stores token-safe OneCLI identity mappings in `onecli_identity_mappings`. Each mapping belongs to one Agency user
and points to a OneCLI agent id plus a server-side `agent_token_secret_ref`. API responses expose only whether the secret
reference is configured, not the reference string or token value.
OneCLI agent ids are unique in Agency so another user cannot register the same gateway identity.

Users can manage their own mappings through:

- `GET /onecli/identity-mappings`
- `POST /onecli/identity-mappings`
- `GET /onecli/identity-mappings/{mapping_id}`
- `PUT /onecli/identity-mappings/{mapping_id}`
- `DELETE /onecli/identity-mappings/{mapping_id}`

Admins can map users or workflows to OneCLI agents through:

- `GET /onecli/admin/identity-mappings`
- `POST /onecli/admin/users/{owner_user_id}/identity-mappings`
- `PUT /onecli/admin/identity-mappings/{mapping_id}`
- `DELETE /onecli/admin/identity-mappings/{mapping_id}`

Mapping create, update, disable, and runtime use paths record token-safe Agency audit actions in runtime operations as
`onecli.identity_mapping.created`, `onecli.identity_mapping.updated`, `onecli.identity_mapping.disabled`, and
`onecli.identity_mapping.used`. Audit payloads include mapping/user/workflow/agent ids and whether a secret reference is
configured. They do not include the OneCLI token or the secret reference value.

New mappings automatically include `metadata.onecli_rule_profile` with the current Agency default rule profile id,
version, bootstrap status, and enabled rule ids. This metadata is not enforcement by itself. Operators must create or
sync the corresponding rules in OneCLI before considering a new user fully provisioned.

Agency disables a user's active OneCLI mappings when:

- `/users/sync` marks the user `disabled`
- an admin disables a user with `DELETE /users/{user_id}`
- the user revokes or deletes an Agency credential whose `secret_ref` is a `onecli://users/{owner_user_id}/...` ref

These paths record token-safe `onecli.identity_mapping.disabled` audit actions with a reason such as `user_disabled`,
`user_deleted`, `credential_revoked`, or `credential_deleted`.

The default rule profile is available through:

- `GET /onecli/rule-profiles/default`
- `GET /onecli/admin/rule-profiles/default`

The initial profile includes:

- blocked destructive email/message deletion for Gmail and Microsoft Graph
- blocked production payment mutations and deletes for Stripe
- blocked broad repository deletion for GitHub and GitLab
- blocked IAM and key-management mutations for Google IAM, Google Cloud KMS, and AWS IAM
- rate limits for Slack message sends, Gmail sends, and GitHub write calls
- a disabled Gmail-send manual-approval template, to enable only after OneCLI approval polling is bridged into Agency

When `agency.http.request` runs with an authenticated Agency actor and `credential_mode=onecli`, Agency uses that user's
active default mapping for OneCLI proxy auth. The global `ONECLI_AGENT_TOKEN_SECRET_REF` fallback is disabled by
default. For single-user local development only, set `ONECLI_ALLOW_GLOBAL_AGENT_TOKEN_FALLBACK=true` to allow fallback
when no user/workflow mapping exists. Fallback usage is token-safe in runtime metadata and recorded as
`onecli.global_agent_token_fallback.used` when Agency has an API context. `ONECLI_MULTI_USER_MODE=true` rejects this
fallback at startup.

## Credential Lifecycle

Use OneCLI as the source of truth for external-service credentials. Agency should keep only token-safe mapping metadata:
Agency user id, optional workflow id, OneCLI agent id, Agency mapping id, rule profile metadata, and whether the
server-side agent token secret reference is configured. Do not store upstream API keys, OAuth access tokens, refresh
tokens, or provider bearer tokens in Agency for proxy-compatible connections.

Add a credential:

1. In OneCLI, create or select the OneCLI agent identity for the Agency user or workflow.
2. Add the external-service credential in OneCLI, using a non-production credential first when validating a new provider.
3. Attach the Agency default rule profile, including destructive denies and rate limits, before exposing the mapping to
   workflows.
4. Store the OneCLI agent token in an Agency server-side secret reference, such as an environment-backed
   `env://ONECLI_AGENT_<USER_OR_WORKFLOW>_TOKEN`. Never put the raw token in API payloads, tool input, worker env vars,
   or connector metadata.
5. Create the Agency identity mapping with `POST /onecli/admin/users/{owner_user_id}/identity-mappings`, or let a user
   create their own mapping through `POST /onecli/identity-mappings`.
6. For connector credentials that already support OneCLI mode, store an owner-scoped
   `onecli://users/{owner_user_id}/...` reference in Agency. Cross-owner refs are rejected by Agency.
7. Validate with a low-risk allowed call, the destructive-rule smoke, and the rate-limit smoke before enabling the
   mapping for production workflows.

Rotate a credential:

1. Create or rotate the upstream credential in OneCLI first. Prefer adding the replacement credential before disabling
   the old one when the provider supports overlap.
2. If the OneCLI agent token also changes, update the Agency server-side secret value behind the existing secret ref.
   If the secret ref name changes, update the mapping with `PUT /onecli/admin/identity-mappings/{mapping_id}`.
3. Keep the Agency mapping id stable when possible so existing audit trails continue to join across rotation events.
4. Run a low-risk OneCLI-routed health check for the affected user or workflow.
5. Review Agency `onecli.identity_mapping.updated` actions and OneCLI gateway logs for the rotation window. Record only
   token-safe ids and timestamps.

Revoke a credential:

1. Revoke or disable the upstream credential in OneCLI when the provider credential is exposed or no longer needed.
2. Disable the Agency mapping with `DELETE /onecli/admin/identity-mappings/{mapping_id}`. For broader actions, use
   `DELETE /onecli/admin/users/{owner_user_id}/identity-mappings` or
   `DELETE /onecli/admin/workflows/{workflow_id}/identity-mappings`.
3. If an Agency credential row references `onecli://users/{owner_user_id}/...`, revoking or deleting that Agency
   credential also disables active owner mappings and records `credential_revoked` or `credential_deleted`.
4. Confirm later tool output and runtime events no longer report the disabled `mapping_id`.
5. Remove or disable the old server-side token secret after OneCLI and Agency both stop using it.

Audit credential use:

1. Start with Agency runtime events and filter for `onecli.http.request.*`,
   `onecli.identity_mapping.created`, `onecli.identity_mapping.updated`, `onecli.identity_mapping.disabled`, and
   `onecli.identity_mapping.used`.
2. Join Agency events to OneCLI gateway logs with `X-Agency-OneCLI-Correlation-ID` and the safe `X-Agency-*` headers.
3. Review Agency user id, workflow id, execution id, mapping id, OneCLI agent id, method, target host, status code,
   gateway decision, rule id or policy name, and timestamp.
4. Do not export raw prompts, request bodies, OneCLI agent tokens, secret refs, upstream credential values, or provider
   response bodies into audit notes.
5. For recurring reviews, sample both successful calls and denied/rate-limited calls so missing rules and stale mappings
   are visible before an incident.

## Agency Diagnostics

Agency exposes a safe OneCLI diagnostic endpoint:

```bash
curl http://localhost:8000/health/onecli
```

When `ONECLI_ENABLED=false`, the route returns the configured URLs and enforcement flags without trying to reach
OneCLI. When `ONECLI_ENABLED=true`, it checks both the dashboard URL and gateway URL with a short timeout.

The response intentionally reports only booleans for sensitive settings:

- whether the gateway CA bundle path is configured
- whether the OneCLI agent token secret reference is configured

It never returns the OneCLI token, secret reference value, encryption key, or upstream service credentials.

## HTTP Tool Mode

`agency.http.request` now accepts:

```json
{
  "credential_mode": "none"
}
```

Supported values:

- `none`: direct HTTP request with no OneCLI proxy routing
- `onecli`: route through `ONECLI_GATEWAY_URL`

When `ONECLI_FORCE_FOR_HTTP_TOOLS=true`, `agency.http.request` defaults to `credential_mode=onecli` even if the agent
omits the field.

When `ONECLI_EXTERNAL_CALLS_DISABLED=true`, Agency denies OneCLI-routed HTTP tool calls before proxying and emits
`onecli.http.request.denied`. This is the global emergency stop for Agency-managed OneCLI external calls.

In production, OneCLI-routed HTTP tool calls fail closed when the gateway/proxy request raises. Agency returns a denied
tool result for contract-backed tool runs and emits `onecli.http.request.failed` with `verdict=deny` and
`fail_closed=true`. Development keeps the softer warning verdict for local troubleshooting, but still does not retry the
request without OneCLI.

In OneCLI mode, Agency rejects agent-provided credential-bearing headers and query parameters before the request is sent.
Blocked headers include `Authorization`, `Proxy-Authorization`, `x-api-key`, `api-key`, `x-goog-api-key`, and
`x-amz-security-token`. Blocked query parameters include `access_token`, `api_key`, `apikey`, `auth_token`,
`client_secret`, `key`, and `token`.

OneCLI mode still runs Agency's HTTP policy first, including host allowlists and mutation warnings. The request is sent
through the configured proxy only after those checks pass.

If `ONECLI_AGENT_TOKEN_SECRET_REF` is configured, Agency resolves it server-side and attaches it to the OneCLI proxy
request using proxy basic auth. Agents cannot provide `Proxy-Authorization` in tool payloads, and Agency does not expose
the token or token secret ref in tool output, runtime events, health checks, or connector responses.

Tool output includes token-safe OneCLI metadata: gateway mode, gateway URL, target scheme/host/port, and the Agency actor
associated with the server-configured OneCLI agent token. It does not include OneCLI token values or upstream credential
values.

For observability, OneCLI-routed HTTP tool calls also emit runtime events with a shared `onecli-http:<uuid>`
correlation id:

- `onecli.http.request.started`
- `onecli.http.request.completed`
- `onecli.http.request.rate_limited`
- `onecli.http.request.failed`
- `onecli.http.request.denied`

When `agency.http.request` runs inside native workflow execution, the same metadata includes safe Agency context:
execution id, workflow id, task id, agent id, and tool call id. Direct `/tools/agency.http.request/run` calls do not
have workflow/task context, so they include only the calling Agency actor.

Agency also forwards safe correlation headers on OneCLI-routed outbound requests so OneCLI gateway logs can be joined
back to Agency records:

- `X-Agency-OneCLI-Correlation-ID`
- `X-Agency-User-ID`, when the runtime has an Agency actor
- `X-Agency-Execution-ID`
- `X-Agency-Workflow-ID`
- `X-Agency-Task-ID`
- `X-Agency-Agent-ID`
- `X-Agency-Tool-Call-ID`

These headers contain only IDs. They do not include OneCLI tokens, secret refs, upstream credentials, prompts, or payload
content.

## Operator Investigation Runbook

Use the Agency runtime events and OneCLI gateway logs together. Start from the Agency `correlation_id` when available;
otherwise filter by Agency user id, workflow id, execution id, target host, and timestamp. Do not copy prompts, tool
payloads, OneCLI tokens, secret refs, or upstream credential values into incident notes.

For suspected prompt injection:

1. Find the related Agency execution, workflow, task, agent, and tool call ids from the conversation or runtime event
   stream.
2. Filter Agency events for the shared `correlation_id` and the `onecli.http.request.*` event family. Include
   `onecli.http.request.denied` and `onecli.http.request.rate_limited`, not only completed requests.
3. Join to OneCLI gateway logs with `X-Agency-OneCLI-Correlation-ID` or the safe `X-Agency-*` headers.
4. Check whether the request host, path, method, and timing match the workflow's intended tool behavior and the
   configured Agency HTTP host policy.
5. If the behavior looks compromised, disable the affected mapping with
   `DELETE /onecli/admin/identity-mappings/{mapping_id}`. For broader account compromise, disable the Agency user with
   `DELETE /users/{user_id}` and revoke or rotate the OneCLI agent token in OneCLI.
6. Preserve the token-safe event ids, mapping id, OneCLI agent id, request method, host, path pattern, status code, and
   decision result for review.

For unexpected external API calls:

1. Filter Agency runtime events by `target_host`, `target_scheme`, status code, Agency user id, and execution/workflow
   ids.
2. Check the tool output `onecli` metadata for the mapped Agency actor and gateway mode. A credentialed external call
   should use `gateway_mode=onecli` when OneCLI enforcement is enabled.
3. Confirm the call was allowed by Agency HTTP policy before it reached OneCLI. Agency-side policy denials emit
   `onecli.http.request.denied` without sending the outbound request.
4. Inspect OneCLI gateway logs for the same correlation id and compare the gateway decision with the Agency event.
5. Review the default rule profile from `GET /onecli/admin/rule-profiles/default` and any user-specific OneCLI rules for
   missing denies, missing rate limits, or rules that are still in `pending_onecli_bootstrap`.
6. Use `GET /health/onecli` only for reachability and configuration state. It is intentionally token-safe and cannot
   prove a specific credential was injected.

For gateway denial or rate-limit events:

1. In Agency, start from `onecli.http.request.denied` or `onecli.http.request.rate_limited`. Rate-limit events are
   warning-severity events and normally correspond to HTTP `429`.
2. Use `correlation_id`, `onecli.agent_identity.mapping_id`, Agency user id, target host, and request method to locate
   the matching OneCLI gateway log entry.
3. Determine whether the decision came from Agency policy before proxying or from OneCLI gateway policy after proxying.
   Agency policy denials protect host allowlists and direct secret-bearing payloads; OneCLI denials protect credential
   injection and gateway rules.
4. If the denial is expected, attach the rule id or policy name to the incident note and leave the credential unchanged.
5. If the denial is unexpected, check whether the mapping was bootstrapped with the current default rule profile and
   whether an operator recently changed OneCLI rules or agent token scope.
6. Do not bypass OneCLI to resolve rate-limit pressure. Adjust the OneCLI rule, split traffic by user/workflow mapping,
   or reduce the workflow's external call volume.

For credential revocation:

1. Revoke or rotate the credential in OneCLI first when the upstream credential itself may be exposed.
2. Disable the Agency mapping with `DELETE /onecli/admin/identity-mappings/{mapping_id}` or let the user disable their
   own mapping with `DELETE /onecli/identity-mappings/{mapping_id}`.
3. If an Agency credential row references `onecli://users/{owner_user_id}/...`, revoking or deleting that Agency
   credential disables active owner mappings and records `onecli.identity_mapping.disabled` with reason
   `credential_revoked` or `credential_deleted`.
4. User suspension and admin user deletion also disable active owner mappings with reasons `user_disabled` or
   `user_deleted`.
5. Verify the mapping is no longer active through `GET /onecli/admin/identity-mappings` and confirm later runtime events
   no longer use that `mapping_id`.
6. Keep the audit trail token-safe: mapping id, Agency user id, OneCLI agent id, reason, operator, and timestamps are
   enough.

For emergency disablement:

1. Set `ONECLI_EXTERNAL_CALLS_DISABLED=true` and restart the Agency API/workers to stop new Agency-managed
   OneCLI-routed HTTP calls before proxying.
2. Disable one user's active OneCLI mappings with
   `DELETE /onecli/admin/users/{owner_user_id}/identity-mappings`.
3. Disable one workflow's active OneCLI mappings with
   `DELETE /onecli/admin/workflows/{workflow_id}/identity-mappings`.
4. Disable a specific mapping with `DELETE /onecli/admin/identity-mappings/{mapping_id}`.
5. Verify later `onecli.http.request.denied` events include `ONECLI_EXTERNAL_CALLS_DISABLED is true` for global stops
   or that mapping lookups no longer return the disabled mapping id for user/workflow stops.

## Isolated Worker Enforcement

Set this only after OneCLI is reachable and the worker network can reach the gateway:

```env
ONECLI_ENABLED=true
ONECLI_FORCE_FOR_ISOLATED_WORKERS=true
EXECUTION_ISOLATION_ENABLED=true
ONECLI_GATEWAY_URL=http://onecli:10255
ONECLI_GATEWAY_CA_BUNDLE_PATH=/host/path/to/onecli-ca.pem
ONECLI_GATEWAY_CA_BUNDLE_CONTAINER_PATH=/etc/agency/onecli/ca.pem
```

When enabled, Agency injects these into isolated worker containers:

- `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy`, `https_proxy`
- `NO_PROXY`, `no_proxy`
- `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE`, `CURL_CA_BUNDLE`, `GIT_SSL_CAINFO`, `NODE_EXTRA_CA_CERTS` when a CA bundle is configured
- safe OneCLI mode metadata, without the agent token or secret reference value

The CA bundle is mounted read-only into the worker at `ONECLI_GATEWAY_CA_BUNDLE_CONTAINER_PATH`.

When `ONECLI_FORCE_FOR_ISOLATED_WORKERS=true`, worker default mounts are locked down to avoid file-based credential
leakage. Whole-workspace mounts and writable Codex home mounts are skipped, the OneCLI CA bundle remains explicit, and
extra mounts with sensitive path markers such as `.env`, `secret`, `credential`, `token`, or `key` fail closed.

Keep `ONECLI_WORKER_NO_PROXY` limited to internal services required by the runtime, such as Postgres, Redis, Agency
backend/control-plane hosts, and the OneCLI gateway/dashboard.

For each isolated worker startup, Agency also records a token-safe `onecli_worker_enforcement` metadata object and emits
`onecli.worker.enforcement.recorded`. This confirms whether proxy env vars, CA trust env vars, and direct external
credential env vars were present in the worker spec without exposing the OneCLI agent token or secret reference value.

Current enforcement mode is `proxy_env_only`: common SDKs and CLIs are steered through OneCLI by environment. Container
level egress controls are still pending, so do not treat this as proof that a malicious process cannot bypass the proxy
until the Docker/network policy phase is complete.

For stricter local Docker enforcement, set:

```env
ONECLI_WORKER_EGRESS_MODE=docker_internal_network
ONECLI_WORKER_EGRESS_NETWORK=agency_onecli_worker_egress
EXECUTION_CONTAINER_NETWORK=agency_onecli_worker_egress
```

The Compose stack creates `agency_onecli_worker_egress` as an internal Docker network and attaches Agency backend,
Postgres, Redis, OneCLI, and OneCLI Postgres to it. Isolated workers launched on that network can reach OneCLI and
runtime services, but should not have direct internet egress. OneCLI remains attached to the default network so it can
reach external services on the worker's behalf.

In production, `ONECLI_FORCE_FOR_ISOLATED_WORKERS=true` requires
`ONECLI_WORKER_EGRESS_MODE=docker_internal_network`.

Validate the worker proxy environment by starting an isolated execution with OneCLI enforcement enabled, then inspect
runtime events and container metadata for `onecli_worker_enforcement`. Confirm proxy variables, CA trust variables, and
the absence of direct external credential env vars without exposing token values.

For HTTPS checks, `ONECLI_GATEWAY_CA_BUNDLE_PATH` must point to a full trust bundle for the worker. Include the worker
base image's normal public roots, the OneCLI gateway CA, and any enterprise TLS inspection roots, such as Cloudflare
Gateway roots, if your network presents them. The bundle is mounted into workers at
`ONECLI_GATEWAY_CA_BUNDLE_CONTAINER_PATH` and used by Python, requests/httpx, curl, git, and Node.

Node 20 `fetch` does not honor `HTTP_PROXY` and `HTTPS_PROXY` by default. Agency sets `NODE_OPTIONS` to preload
`app/runtime/node_onecli_proxy.cjs`, which installs an Undici `ProxyAgent` for Node `fetch`. Rebuild the worker image
after changing this file. For git-specific proxy validation, use a public read-only remote from inside an isolated
worker and confirm the request routes through OneCLI. For internal-network bypass validation, run with
`ONECLI_WORKER_EGRESS_MODE=docker_internal_network` and confirm direct external egress fails while OneCLI-routed egress
still works.

## Gateway Rule Smoke Tests

Before enabling OneCLI for multi-user credentials, configure a non-production OneCLI agent with the Agency default rule
profile and at least one explicit destructive deny rule. The gateway should return HTTP `403` for a destructive endpoint
before the request reaches the upstream provider.

Default destructive smoke target:

```text
DELETE https://gmail.googleapis.com/gmail/v1/users/me/messages/agency-onecli-smoke-delete
```

The message id is intentionally a fake smoke id. A correctly configured OneCLI rule should deny the request before
credential injection or upstream delivery. If the smoke observes any status other than the expected gateway denial, treat
the rule as missing or mis-scoped until the OneCLI gateway logs prove otherwise.

Run the live smoke with a non-production agent token and a deliberately fake or disposable destructive target. Keep the
target method, URL, expected status, and OneCLI gateway logs with the validation record.

Result interpretation:

- `403`: expected destructive-rule denial.
- `401` or `407`: gateway auth or agent token setup problem.
- `429`: a rate-limit rule fired before the destructive deny; use the rate-limit smoke instead.
- Any upstream-looking `2xx`, `404`, provider-specific `403`, or provider error: inspect OneCLI gateway logs and fix
  the deny rule before using the mapping for real workflows.

For rate limits, configure a low threshold on a harmless test URL for the same non-production OneCLI agent. The smoke
passes only when one of the repeated requests receives the expected gateway denial, normally HTTP `429`.

Default rate-limit smoke target:

```text
GET https://example.com/agency-onecli-rate-limit-smoke
```

Run the live smoke after creating the rate-limit rule. Record the test URL, method, attempt count, expected status, and
the first response that proves the rule fired.

Result interpretation:

- `429`: expected rate-limit denial.
- `401` or `407`: gateway auth or agent token setup problem.
- Repeated upstream-looking statuses without `429`: the OneCLI rate-limit rule does not match this method, host, path,
  or agent identity.
- `403`: another deny rule fired first; adjust the smoke URL or rule ordering so the rate-limit path is tested
  separately from destructive denies.

## Backup And Restore Validation

Validate OneCLI backup and restore handling before using the gateway for multi-user credentials. Create a custom
`pg_dump` archive from `onecli-postgres`, restore it into a temporary scratch database in the same service, verify that
public tables exist, then drop the scratch database. Also archive and list `/app/data` from the OneCLI app data volume.
Do not restore over the live `onecli` database.

Kept artifacts are written under `.data/onecli-backup-validation/` and may contain encrypted or otherwise sensitive
credential data. Do not commit them.

## Initial Operator Steps

1. Open `http://127.0.0.1:10254`.
2. Create or complete the initial OneCLI operator setup.
3. Create an Agency development agent identity.
4. Add one low-risk test credential, such as a non-production GitHub or test HTTP API credential.
5. Add one deny rule for a known test path.
6. Add one rate-limit rule for a low-risk endpoint.
7. Run a destructive-rule smoke with a non-production agent token and confirm the expected HTTP `403`.
8. Run a rate-limit smoke with a non-production agent token and confirm the expected HTTP `429`.
9. Confirm gateway logs show allowed, denied, and rate-limited requests without printing credential values.
10. Run a backup/restore validation and record the validation timestamp.

## Agency Migration Boundary

Agency now has the first OneCLI integration slices:

- OneCLI service bring-up through the Compose profile.
- Agency settings, diagnostics, and `/health/onecli`.
- `agency.http.request` OneCLI routing with direct auth-header/query-param rejection.
- Isolated worker proxy injection, Node `fetch` bootstrap, and optional internal Docker egress network.
- Connector health and outbound delivery migration for eligible header-auth providers.

Remaining implementation phases:

- migrate remaining provider credentials provider by provider
- migrate LLM provider credentials where compatible
- remove Agency external-service credential storage after replacement tests pass

Until those phases land, do not delete existing Agency connector credentials or direct `secret_ref` code paths.

## Connector Setup Sessions

Connector setup is backend-owned by Agency and backed by OneCLI refs. The FE and CLI are clients of the same Agency
lifecycle:

- `POST /integrations/connectors/{provider}/setup-sessions`
- `GET /integrations/connectors/installations`
- `GET /integrations/connectors/installations/{installation_id}`
- `POST /integrations/connectors/installations/{installation_id}/complete`
- `POST /integrations/connectors/installations/{installation_id}/test`
- `POST /integrations/connectors/installations/{installation_id}/rotate`
- `DELETE /integrations/connectors/installations/{installation_id}`

Setup and rotation responses include only token-safe fields: the Agency installation id, Agency user id, OneCLI setup URL,
device code, provider key, and owner-scoped `onecli://users/{owner_user_id}/...` credential ref. Until OneCLI exposes a
dedicated connector setup route, Agency points `setup_url` at the OneCLI dashboard root with token-safe query parameters
instead of a provider-specific path. The setup URL includes `agency_user_id` and `onecli_credential_ref` so OneCLI can
prefill or persist the custom connection under Agency's namespace instead of deriving storage from the signed-in OneCLI
account. Agency rejects raw secret-shaped payload keys and non-owner-scoped OneCLI refs.

Telegram still uses the same setup flow, but only for secret storage. The UI should surface that Telegram delivery and
health checks remain direct because the bot token is embedded in the request path, not a header. Direct transport still
uses an Agency runtime secret mirror for the same token so OneCLI stays the setup/storage layer for the direct path.
When the launcher exports `AGENCY_PUBLIC_WEBHOOK_BASE_URL`, Telegram completion auto-registers the webhook against that
public URL so operators do not have to run `setWebhook` manually during the normal launcher flow. If the setup payload
does not include a Telegram webhook secret token, Agency generates one and stores it with the installation metadata.
Telegram metadata must include the numeric `bot_user_id` from the bot token's `getMe` response and the bot
`bot_username` without the `@` prefix.

Compatibility rule of thumb:

- `direct-only`: Telegram, because the bot token is part of the URL path and OneCLI cannot inject it through the proxy.
- `direct-capable`: Discord, because Agency can mirror the same secret into runtime storage and use the Discord Gateway
  path for server/channel chat and DMs.
- `proxy-compatible`: WhatsApp, Slack, Microsoft Teams, Twilio, and other header-based or query-based HTTP
  integrations.

Use OneCLI as the credential source and setup flow. Only the provider transport should differ between the two modes.
For `direct` connectors, Agency mirrors the same secret into a runtime secret record at completion time so direct
delivery and health checks can stay off the OneCLI proxy path. The direct request path bypasses inherited proxy env
vars so the provider call stays direct even when the launcher or container has OneCLI-related proxy settings present.

CLI setup uses the same service and payload shape as the API:

```bash
python -m app.cli connector setup telegram --owner-user-id user-123 --json
python -m app.cli connector list --owner-user-id user-123 --json
python -m app.cli connector status <installation-id> --owner-user-id user-123 --json
python -m app.cli connector complete <installation-id> --owner-user-id user-123 --json
python -m app.cli connector rotate <installation-id> --owner-user-id user-123 --json
python -m app.cli connector revoke <installation-id> --owner-user-id user-123 --json
```

For providers that need delivery metadata, pass non-secret metadata through `--metadata-json`, for example:

```bash
python -m app.cli connector setup whatsapp \
  --owner-user-id user-123 \
  --metadata-json '{"phone_number_id":"1234567890"}' \
  --json
```

Non-FE clients such as Discord, Telegram, and assistant tools should call the same backend routes or service layer and
return the setup URL/device code to the user. They must not ask users to paste upstream provider tokens into chat.

## Connector Health Migration

Connector credentials can now use `onecli://...` as a mapping reference for eligible health checks. In this mode Agency
does not resolve a raw secret and does not send provider auth headers. It routes the health check through
`ONECLI_GATEWAY_URL` and returns token-safe metadata in the response.

User-submitted OneCLI refs must use an owner-scoped, installation-scoped path such as
`onecli://users/{owner_user_id}/{provider}/{agency_installation_id}`. The backend rejects cross-owner refs during
credential create, update, and rotate. This keeps multiple bots or connectors for the same provider separate without
colliding on a shared `/default` slot.

Current support:

- `discord-bot`: direct-capable for health checks and delivery.
- `whatsapp-cloud-api`: supported for health checks and delivery.
- `slack-app`: proxy-compatible for header-based webhook and API calls.
- `microsoft-teams`: proxy-compatible for header-based webhook and API calls.
- `twilio-sms`: proxy-compatible for header-based SMS and voice API calls.
- `telegram-bot`: direct-only because the Telegram Bot API token is part of the URL path; Agency mirrors the token
  into a runtime secret record during completion so OneCLI stays the setup/storage layer.

The Telegram setup guide intentionally includes a note for the frontend: store the bot token in OneCLI, but do not show
OneCLI proxy routing as available for Telegram delivery or health checks. The direct path mirrors the same token into
an Agency runtime secret record so Telegram can remain direct while OneCLI keeps the setup flow. The launcher-provided
public webhook base URL lets Agency register the Telegram webhook automatically during completion. The credential
metadata should use the numeric Telegram `bot_user_id` from `getMe`, not the bot display name.

## Connector Delivery Migration

Channel outbound delivery now supports `onecli://...` credential references for eligible header-auth providers.
Discord is direct-capable because Agency can mirror the same token into a runtime secret record and use the Discord
Gateway path for server/channel chat and DMs. WhatsApp, Slack, Microsoft Teams, and Twilio are proxy-compatible
because their credentials can be injected as headers or otherwise routed without putting the secret in the URL path.
Telegram delivery stays direct, with Agency mirroring the same token into a runtime secret record instead of routing
Telegram through OneCLI.

Keep provider-specific migration status next to the connector implementation and tests as providers are moved into
OneCLI mode.

## Discord Transport Limitation

With the published OneCLI image, Agency can use direct-capable Discord credentials for:

- interaction webhooks
- outbound delivery
- server/channel polling for ordinary chat

It can provide Discord DM chat in that mode when the credential is completed in direct mode. Document the distinction
in setup guides so operators choose the right transport mode for the connector.
