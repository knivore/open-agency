# Unified Browser Capability

Agency exposes one `agency.browser.*` family for both web retrieval and interactive browsing. `agency.browser.open` uses
Patchright first, extracts the loaded DOM, and either closes all resources or returns an owner-scoped live `session_id`.
Scrapling 0.4 is a bounded last resort and is not exposed as a tool choice.

## Choosing the lifecycle

- Use `keep_open=false` to read one page efficiently. The page, context, browser process, and temporary profile are
  closed before the result is returned.
- Use `keep_open=true` (the default) when the next step may scroll, click, type, take a screenshot, follow a link, repeat
  extraction, or require a human challenge handoff.
- Pass the returned `session_id` to every later browser action. Omitting it is accepted only when the owner has exactly
  one live session.
- Retained sessions expire after 15 idle minutes or 60 total minutes by default. Terminal executions close their
  sessions; durable human/approval waits preserve them until the TTL.

## Open and extraction inputs

`agency.browser.open` accepts:

- `url`: the explicit HTTP(S) navigation target;
- `goal`: an optional research or extraction goal;
- `extract_mode`: `auto`, `text`, `markdown`, `article`, `html`, or `none`;
- `keep_open`: whether to retain a controllable page;
- `session_id`: an existing owned session for another navigation;
- browser context options such as locale, timezone, viewport, device scale, mobile mode, user agent, storage state,
  tracing, and video;
- `proxy_binding`: an opaque Agency credential binding. Raw proxy credentials are never accepted as model-visible
  arguments;
- `runtime_policy`: optional per-open preferences for session TTLs, session limits, navigation timeout, retries, domain
  concurrency/pacing, and artifact retention.

The agent can use `runtime_policy` to match resources to the work—for example, a short one-page read can request one
attempt and a short timeout, while manual exploration can request a longer retained session. Environment values remain
the local operator defaults and ceilings: a request may reduce resource use, but cannot exceed capacity/retention caps or
lower the operator's minimum per-domain interval. Session creation fields do not rewrite an already retained session.

| Agent `runtime_policy` field | Local environment boundary | Effective behavior |
| --- | --- | --- |
| `session_idle_ttl_seconds` | `BROWSER_SESSION_IDLE_TTL_SECONDS` and maximum TTL | Per-session idle lifetime; may extend the default only within maximum lifetime |
| `session_maximum_ttl_seconds` | `BROWSER_SESSION_MAXIMUM_TTL_SECONDS` | Hard per-session lifetime cap |
| `max_sessions_per_owner` / `max_sessions_total` | `BROWSER_SESSION_MAX_PER_OWNER` / `BROWSER_SESSION_MAX_TOTAL` | Stricter admission limits for this new session |
| `navigation_timeout_ms` | `BROWSER_NAVIGATION_TIMEOUT_MS` | Per-navigation timeout up to the local cap |
| `retry_attempts` | `BROWSER_PATCHRIGHT_ATTEMPTS` | Patchright attempts up to the local cap |
| `domain_max_concurrency` | `BROWSER_DOMAIN_MAX_CONCURRENCY` | Per-domain concurrency up to the local cap |
| `domain_min_interval_seconds` | `BROWSER_DOMAIN_MIN_INTERVAL_SECONDS` | Per-domain pacing no faster than the local floor |
| `artifact_retention_seconds` | `BROWSER_ARTIFACT_RETENTION_SECONDS` | Per-artifact retention up to the local cap |

Infrastructure teardown, sidecar transport, health-check, and expiry-scan timeouts remain operator-only because changing
them inside one page request could destabilize shared runtime cleanup rather than improve that page's work.

The versioned `agency.browser.v1` response contains status, requested and final URLs, title, engine, extraction,
structured challenge data, timings, artifacts, and—only for a retained controllable page—`session_id` with
`interactive=true`. Extraction includes readable text, Markdown, canonical URL, article metadata, and absolute links as
applicable to the selected mode.

## Examples

Read an article without retaining a browser:

```json
{
  "url": "https://example.com/article",
  "goal": "Summarize the main claims and source links",
  "extract_mode": "article",
  "keep_open": false
}
```

Explore and refresh extraction:

1. Call `agency.browser.open` with `keep_open=true`.
2. Pass its `session_id` to `agency.browser.scroll`, `agency.browser.click`, or `agency.browser.type-text`.
3. Call `agency.browser.extract-screenshot` with the same `session_id`; despite the retained legacy tool ID, it now
   performs DOM extraction from the current page state.
4. Close with `agency.browser.close`.

For manual crawling, a human can request screenshots, use semantic click/type actions, or supply screenshot-relative
`x` and `y` coordinates to `agency.browser.click`. This operates the same retained page the agent uses, so no state,
cookies, proxy identity, or challenge clearance is lost between human and agent steps.

## Challenges and CAPTCHA handoff

Challenge results identify the kind, confidence, evidence indicators, HTTP status, final URL, engine, retryability,
terminal state, and whether human action is required. Recovery is bounded: normal Patchright load, a focused visible
checkbox attempt, fresh Patchright contexts, then Scrapling. There is no infinite retry loop.

Agency does not promise universal CAPTCHA solving. Visual puzzles, OTPs, account verification, and device confirmation
return `human_action_required`. When `keep_open=true`, the response contains the same live `session_id`, an owner-scoped
screenshot artifact, expiry time, instructions, and `agency.human.ask` as the pause/handoff tool. After the operator
finishes, navigate or extract with the same `session_id`. Unattended handoffs expire normally.

Adding a third-party CAPTCHA provider requires a separate approval, credential, privacy, and terms review. Crawling and
challenge interaction must be authorized and comply with target terms and applicable policy.

## Proxy configuration

`BROWSER_RUNTIME_PROXY_BINDINGS_JSON` maps opaque binding names to one proxy URL or a list representing a managed pool.
The environment value is a runtime secret and must come from Agency credential deployment, not a workflow prompt.
Bindings support HTTP, HTTPS, and SOCKS5 endpoints with optional authentication. Per-domain assignments remain sticky
for a bounded request count and TTL, rotate only after evidence of transport/block failure, and expire with domain
history. Results, events, health output, and logs expose neither endpoints nor credentials.

Direct mode is used when `proxy_binding` is absent or equals `direct` and operator policy permits it.

## Security boundaries

- Each navigation grants only its explicit public origin plus hosts injected by trusted policy; there is no wildcard.
- URL policy rejects credentials in URLs and loopback, private, link-local, metadata, reserved, unresolved, or
  unapproved DNS answers. The runtime rechecks redirects and every browser request, including frames, popups, and
  subresources.
- Downloads are disabled until an Agency malware-scanning sink is configured.
- Workers receive an execution-derived signing key, not the browser-runtime master key. One-time capabilities bind the
  operation, owner, audience, expiry, and approved hosts.
- Sessions and artifacts require matching execution/workspace/user/actor ownership. Session IDs are opaque and do not
  grant access by themselves.
- Authorization headers, cookies, passwords, form values, storage state, proxy secrets, and failure HTML are redacted
  or stored as owner-scoped artifacts rather than logged or returned directly.
- Browser mutations retain normal Agency approval policy.

## Deployment

The `browser-runtime` Compose service uses [its dedicated image](../docker/browser-runtime/Dockerfile) with Patchright
1.56.0, Playwright 1.56.0 (required internally by Scrapling), Scrapling 0.4, and the matching Chromium binary. Agency's
backend does not install Playwright or Chromium; the dependency sets do not share a Python environment.

`./agency start` generates a random `BROWSER_RUNTIME_SIGNING_SECRET` of at least 32 characters when `.env` does not
already contain a valid one. It is a local trust key shared only by Agency and the browser runtime, not a third-party API
credential. Keep it distinct from `AGENCY_INTERNAL_API_KEY`; workers receive derived, execution-scoped keys rather than
the master secret. The maintained defaults are:

- idle/max session TTL: `900` / `3600` seconds;
- per-owner/total sessions: `3` / `8`;
- container memory/CPU/PID limits: `4g` / `4.0` / `512` with `1gb` shared memory;
- navigation timeout: `45000` ms;
- resource cleanup timeout: `5` seconds per engine resource;
- Patchright attempts: `3`;
- Scrapling enabled: `true`;
- domain concurrency: `2`;
- artifact retention: `86400` seconds.

Kill switches are `BROWSER_UNIFIED_ENABLED`, `BROWSER_UNIFIED_EXTRACTION_ENABLED`,
`BROWSER_CHALLENGE_HANDLING_ENABLED`, and `BROWSER_SCRAPLING_ENABLED`. Patchright is mandatory and has no enable/disable
environment variable.
`BROWSER_IGNORE_HTTPS_ERRORS` exists only for explicitly approved local TLS-inspection environments and must remain
false otherwise. Headful mode additionally requires an operator-provided display server; container defaults are
headless.

Health at `/health` verifies exact package versions, the Chromium executable, runtime storage/free space, active session
count, and independent Patchright/Scrapling availability.

Publish immutable browser-runtime image tags for local release candidates and set `BROWSER_RUNTIME_IMAGE` to the approved tag.
Rollback means restoring the preceding compatible image tag and restarting only `browser-runtime`; do not roll back the
database or unrelated Agency services. Use the independent Scrapling and challenge-handling kill switches for a narrower
rollback when Patchright browsing itself remains healthy.

Set `BROWSER_RUNTIME_RELEASE` to the deployment or Git revision and keep `BROWSER_RUNTIME_IMAGE` immutable. `/health`
reports both identifiers plus live container cgroup memory/PID gauges and portable runtime/Chromium process-tree
fallback gauges, making it possible to prove which release was exercised and compare resource use while a retained page
is active.

### Local release evidence

Use only targets explicitly approved for automation. For this local-only Agency installation, the maintained local
deployment is the validation and release target; no separate staging environment is assumed. Capture the incumbent
baseline before changing its image:

```bash
./.venv/bin/python scripts/browser_rollout_check.py capture \
  --label incumbent-2026-07-22 \
  --url https://approved-content.example/article \
  --challenge-url https://approved-challenge.example/test \
  --challenge-kind turnstile \
  --human-wait-seconds 300 \
  --output /tmp/browser-incumbent.json
```

The challenge command prints the retained session ID and handoff instructions to stderr. The operator completes the
verification through the Agency browser surface; the check then proves that extraction resumed on that same session and
that owner-scoped sessions were closed. It does not solve or bypass CAPTCHA automatically.

Deploy the candidate under an immutable image tag and unique `BROWSER_RUNTIME_RELEASE`, capture the same scenarios, then
gate it against the baseline:

```bash
python scripts/browser_rollout_check.py capture \
  --label candidate-2026-07-22 \
  --url https://approved-content.example/article \
  --challenge-url https://approved-challenge.example/test \
  --challenge-kind turnstile \
  --human-wait-seconds 300 \
  --output /tmp/browser-candidate.json

python scripts/browser_rollout_check.py compare \
  --baseline /tmp/browser-incumbent.json \
  --candidate /tmp/browser-candidate.json \
  --expected-release candidate-2026-07-22
```

The gate compares identical scenarios, success rate, mean wall latency, fallback/challenge rates, challenge recovery,
cleanup failures, container memory, and PIDs. Default tolerances allow at most a 2-point success-rate drop, 1.5x
latency/resource growth, and 10-point fallback/challenge-rate increases. Tighten them with the command flags when the
staging sample is large enough.

Exercise the candidate against a small approved local workload first. If the gate or live metrics fail, first use the
narrow `BROWSER_SCRAPLING_ENABLED=false` or `BROWSER_CHALLENGE_HANDLING_ENABLED=false` switches where appropriate. For a
full rollback, restore the incumbent immutable image/release, restart only `browser-runtime`, recapture
the same scenarios as `/tmp/browser-rollback.json`, and prove restoration:

```bash
python scripts/browser_rollout_check.py verify-rollback \
  --baseline /tmp/browser-incumbent.json \
  --rollback /tmp/browser-rollback.json \
  --expected-release incumbent-2026-07-22
```

Archive all three records and comparison output with the deployment. A successful command is the evidence required to
check the local comparison and rollback items; the existence of this harness alone is not rollout evidence.

For the one-time migration from the former in-process Playwright browser, `scripts/browser_incumbent_check.py` and
`docker/browser-runtime/Dockerfile.incumbent-validation` build an isolated historical checkout and emit the same rollout
record schema. This keeps the comparison reproducible without restoring legacy browser modules to the application.

## Troubleshooting

- `Browser runtime signing secret is not configured`: start through `./agency start` so the launcher generates and shares
  the local master secret. If Compose was started directly, run the launcher once or provide one 32+ character value in
  `.env`; isolated workers receive derived keys automatically.
- `host ... is not approved`: add the exact required origin through trusted workflow/domain policy. Do not use a global
  wildcard merely to permit third-party page assets.
- Chromium certificate errors: install the organization CA in the runtime image. Use the development-only HTTPS-ignore
  switch only for an approved local test.
- Patchright failures followed by Scrapling: inspect the structured `diagnostics.attempts`, challenge details, and
  owner-scoped screenshot/HTML artifacts.
- `interactive=false` with `keep_open=true`: this is an explicit failure; Scrapling content was retrieved but state
  handoff could not be verified. Retry or request human/operator action rather than treating it as a live session.
- Missing session: it expired, the execution ended, the browser crashed, or it belongs to another owner. Open a new one.

Deterministic tests use stored HTML and fake adapters. Opt-in live validation should target only approved domains and is
reported separately because anti-bot and network behavior are environmental.

Run the deterministic in-image integration matrix after building the runtime:

```bash
docker run --rm --shm-size=1gb agency-browser-runtime:latest \
  python -m app.browser_runtime.selftest
```

`make check-browser-runtime` builds the exact dedicated image and runs this matrix with the maintained container limits;
the PR smoke workflow runs the same target as an independent CI job.

It exercises normal HTML, JavaScript rendering, redirects, popup adoption, repeated navigation, automatic checkbox
challenge recovery, same-session human challenge completion, blocked downloads, wall-clock navigation timeout, trace
shutdown, three concurrent browser sessions, and profile cleanup against a process-local fixture server. The fixture's
loopback exception exists only in the self-test subclass and does not add a production private-network bypass.

For an environment-dependent check, start the runtime and pass one explicitly approved public URL:

```bash
python scripts/browser_live_check.py --url https://example.com
```

An approved challenge target can additionally use `--expect-challenge` and optional `--challenge-kind`. Never point the
live check at a target you are not authorized to access or automate.
