# LLM Models

The FE presents LLM setup as one `LLM Models` area. The primary action is `Add LLM model`, which can either attach a new
model preset to an existing connection or create the connection and preset together.

The backend still stores two related records because they answer different runtime questions.

## LLM Connections

An LLM connection answers how the backend reaches a provider or local runtime.

Examples:

- `OpenAI`
- `Anthropic`
- `Google Gemini`
- `Ollama Local`
- `HuggingFace TGI`
- `OpenAI-compatible Gateway`

Backend provider records are managed through:

- `GET /model-providers`
- `POST /model-providers`
- `PUT /model-providers/{provider_id}`
- `DELETE /model-providers/{provider_id}`

Provider records hold connection-level settings such as provider family, endpoint URL, and backend config.

When a connection is edited from the FE, the UI keeps linked presets in sync by refreshing the copied endpoint and
credential reference fields on every model preset that references that provider id. The provider id itself is
intentionally not editable because existing presets, agents, and workflows may reference it.

Custom `openai_compatible` and `ollama` endpoints must match `MODEL_PROVIDER_ALLOWED_HOSTS`. Official provider
families require HTTPS and remain pinned to their known service hosts so a profile cannot redirect an ambient provider
credential to an attacker-selected destination. Local gateway keys are bound to the exact configured local endpoint,
and runtime overrides do not inherit a saved key when the provider or endpoint changes.

### OAuth-backed Connections

For OAuth-capable providers such as `openai_codex`, the FE should treat OAuth as connection state, not model preset
state.

- store provider OAuth status on the connection card, not on each model preset
- treat `provider.config.auth_profiles` as the canonical token sink
- treat `provider.config.default_oauth_profile_id` as the default active account for the connection
- allow a model preset to optionally set `parameters.oauth_profile_id` when it should bind to a non-default account
- do not copy `access_token`, `refresh_token`, `expires_at`, or `account_id` into every profile in the UI

Recommended UI shape for an OAuth-backed provider connection:

- provider family selector
- base URL
- auth mode selector: `ChatGPT OAuth` or `API key`
- connection-level OAuth status
- one or more logical OAuth profiles/accounts
- actions for `Authorize`, `Complete`, `Device Code`, `Set Default`, and `Re-authorize`

For `openai_codex`, `ChatGPT OAuth` and `API key` are separate runtime paths:

- `API key` uses the public OpenAI API at `https://api.openai.com/v1` and is suitable for Docker, LAN mode, and direct
  backend LLM calls.
- `ChatGPT OAuth` uses Codex product authentication. Direct public OpenAI API calls cannot use these tokens, so backend
  chat requires the Codex CLI in the backend runtime. For local Codex-first chat, run `./run.sh start` so the
  main-agent LLM call uses the host Codex CLI and host `~/.codex`. Fully Dockerized runs still need Codex CLI and auth
  inside the backend/worker container, or should use API-key mode, Ollama, or another OpenAI-compatible provider.

For `openai_codex`, each OAuth profile conceptually maps to one OpenAI account. The FE should display:

- `auth_profile_id`
- `account_id` when available
- whether the profile is the default connection profile
- whether the token is active or likely expired

The FE should also preserve backward compatibility with old provider records that only have top-level
`access_token` / `refresh_token` fields and no `auth_profiles` map yet. Those should be rendered as one implicit
default OAuth profile.

## Model Presets

A model preset answers which model to use and with what runtime defaults.

Examples:

- `Ollama Main` using `llama3:8b`
- `OpenAI Fast` using `gpt-4.1-mini`
- `Claude Planning` using `claude-sonnet-4-5`
- `Gemini Long Context` using `gemini-2.5-pro`

Backend model profile records are managed through:

- `GET /model-profiles`
- `POST /model-profiles`
- `PUT /model-profiles/{profile_id}`
- `DELETE /model-profiles/{profile_id}`

Profiles hold the selected provider id, model id, endpoint override, credentials reference, temperature, max tokens, top
p, and capability flags.

### Model Fallbacks

Model fallbacks are configured on each model preset. They let a run continue with a backup model when the primary model
fails for an allowed retryable reason such as rate limiting, timeout, temporary provider unavailability, network
failure, or auth/access errors.

Fallback settings live on the model profile:

- `fallback_strategy`: one of `auto`, `manual`, or `disabled`
- `fallback_models`: ordered list of up to five manual backup targets
- `fallback_policy`: rules that decide which failures and targets are eligible

The frontend exposes these settings in `LLM Models` -> `Edit preset` -> `Fallback models`.

Strategy behavior:

- `auto`: the runtime chooses up to two curated provider-default backup models and filters them through the capability
  policy.
- `manual`: the runtime tries the configured `fallback_models` in order.
- `disabled`: the runtime does not switch models; the primary model error fails the current model call.

Manual fallback targets can override provider, model, endpoint, auth reference, tuning, context window, capability
flags, and provider-specific parameters:

```json
{
  "fallback_strategy": "manual",
  "fallback_models": [
    {
      "provider": "provider-openai",
      "model": "gpt-4o-mini"
    },
    {
      "provider": "provider-anthropic",
      "model": "claude-3-5-haiku-latest",
      "supports_tools": true,
      "supports_vision": true
    }
  ]
}
```

Fallback policy controls when a switch is allowed:

```json
{
  "fallback_policy": {
    "retry_on": ["rate_limit", "timeout", "service_unavailable", "network"],
    "same_provider_only": false,
    "require_capability_match": true
  }
}
```

`retry_on` supports:

- `rate_limit`: quota/rate-limit errors such as HTTP 429
- `timeout`: request timeout and timeout-like HTTP statuses
- `service_unavailable`: temporary provider/service failures such as HTTP 500, 502, or 503
- `network`: connection and network transport failures
- `auth`: access failures such as HTTP 401 or 403

`same_provider_only=true` rejects manual targets that specify a different provider id from the primary profile.
`require_capability_match=true` requires backup targets to satisfy the primary profile's enabled capability flags:

- `supports_tools`
- `supports_structured_output`
- `supports_vision`
- `supports_streaming`

Auto fallback uses a curated capability catalog for known provider defaults. Manual custom targets can provide explicit
capability flags in `fallback_models`; when a manual custom target does not provide flags, the runtime allows it and
assumes the operator has verified compatibility.

Complete profile update example:

```http
PUT /model-profiles/{profile_id}
Content-Type: application/json

{
  "name": "OpenAI Fast",
  "provider": "provider-openai",
  "model": "gpt-4.1-mini",
  "supports_tools": true,
  "supports_structured_output": true,
  "supports_vision": false,
  "supports_streaming": true,
  "fallback_strategy": "manual",
  "fallback_models": [
    {"provider": "provider-openai", "model": "gpt-4o-mini"},
    {"provider": "provider-anthropic", "model": "claude-3-5-haiku-latest"}
  ],
  "fallback_policy": {
    "retry_on": ["rate_limit", "timeout", "service_unavailable", "network"],
    "same_provider_only": false,
    "require_capability_match": true
  }
}
```

Troubleshooting:

- No switch happened: confirm `fallback_strategy` is not `disabled` and the failure category is present in
  `fallback_policy.retry_on`.
- A manual backup was skipped: check `same_provider_only` and capability flags.
- Auto fallback produced fewer than two backups: compatible curated targets may not exist for the primary provider and
  enabled capabilities.
- Fallback exhausted: every eligible target failed; inspect `model.fallback.failed` events for attempt-level errors.

For OAuth-backed providers, profiles should not own the tokens themselves. The only OAuth-specific profile field the FE
should edit is:

```text
parameters.oauth_profile_id
```

Use that only when the user wants a specific model preset to bind to a non-default OAuth account. Otherwise leave it
unset and let the runtime use the provider connection's `default_oauth_profile_id`.

## Ollama

For local Ollama:

1. Run Ollama locally.

```bash
ollama serve
ollama pull llama3:8b
```

2. Create an `Ollama` LLM connection with:

```text
provider_type = ollama
base_url = http://localhost:11434
```

When the backend runs on the host with `./run.sh start`, use `http://localhost:11434`. When the backend runs
inside Docker and Ollama runs on the host machine, `localhost` points at the backend container itself; in that setup,
prefer `http://host.docker.internal:11434`.

Large local models can take longer than cloud APIs, especially on the first request while Ollama loads the model. Docker
Compose defaults `LLM_REQUEST_TIMEOUT_SECONDS` to `180`; override it in `.env` if your hardware or model needs more
time. Runtime executions resolve this value into `execution.metadata.runtime_policy`, where workflow, task, agent,
trigger, or input metadata can also set `llm_request_timeout_seconds`. A single model profile can still set
`request_timeout_seconds` in its parameters to override the app default for that profile.
For Qwen thinking models, set `think` to `false` in profile parameters when you want normal chat responses instead of a
separate hidden reasoning field.

3. Create one or more model presets on that connection:

```text
model = llama3:8b
model = mistral:7b
model = qwen2.5:14b
```

4. Switch the main agent to the chosen profile with:

```http
PATCH /conversations/main-agent-profile
Content-Type: application/json

{
  "default_model_profile_id": "your-profile-id"
}
```

The frontend `LLM Models` screen provides guided provider presets for common provider families and copies provider
endpoint settings into new presets so runtime clients resolve correctly.

## Embedding Model Profiles

Durable memory vector retrieval uses the same model-profile system as chat models. Configure an embedding-capable
profile, then set:

```bash
MEMORY_EMBEDDING_MODEL_PROFILE_ID=your-embedding-profile-id
```

Supported embedding paths:

- OpenAI-compatible profiles call `/v1/embeddings`.
- Ollama profiles call `/api/embed`; local embedding models such as `nomic-embed-text` or `mxbai-embed-large` should be
  pulled into Ollama before use.

After setting the env var, run `POST /memories/embeddings/backfill` to embed existing memories. New memory writes embed
automatically when the profile resolves successfully.

## FE Surfaces

Use the `LLM Models` screen for setup and edits:

- add an LLM model in one flow
- reuse an existing LLM connection or create a new connection inline
- edit existing LLM connections, including name, family, endpoint URL, and API key
- manage OAuth login state at the connection level for OAuth-backed providers
- let presets optionally choose which OAuth profile/account they should use
- edit model ids, endpoint/auth references, tuning, and capability flags
- choose the profiles that agents can later bind to

### OpenAI Codex OAuth Flow

The FE behavior for `OpenAI Codex (OAuth)` should be:

1. Create or load the provider connection.
2. Let the user choose or create a logical `auth_profile_id`.
3. Call `POST /model-providers/{provider_id}/authorize` with optional `auth_profile_id`.
4. Open the returned `auth_url` in the browser.
5. If loopback capture succeeds, call `POST /model-providers/{provider_id}/callback-complete` with:

```json
{
  "code": "...",
  "pkce_verifier": "...",
  "state": "...",
  "auth_profile_id": "..."
}
```

6. If the browser lands on a redirect page that cannot be captured locally, let the user paste the full redirect URL
   and call the same endpoint with:

```json
{
  "redirect_url": "http://127.0.0.1:1455/auth/callback?code=...&state=...",
  "auth_profile_id": "..."
}
```

7. For headless or blocked loopback environments, call `POST /model-providers/{provider_id}/device-authorize`, show
   the verification URI and user code, then call `POST /model-providers/{provider_id}/device-complete`.

After completion, refresh the provider record and render the returned OAuth profile/account metadata from provider
config.

Use the `Integrations` screen as inventory:

- inspect configured LLM connections with their attached model presets grouped underneath
- inspect non-chat integrations such as tools, MCP servers, and runtime adapters
- manage custom integrations where the backend exposes dedicated operations

LLM mutation intentionally lives in `LLM Models` so provider/model setup has one primary workflow.
