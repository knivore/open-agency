# Open Agency

Open-source control plane for operating real agent systems.

Open Agency gives teams the backend layer agents need after the prototype: workflows, memory, tools, approvals, observability, channels, and local-first setup in one system.

`Multi-agent orchestration · Memory · Tools · Approvals · Local + Cloud Models · MCP + A2A · Open Source`

## Opening Hook

Most agent projects start with a model call and a few tools. Then the real work begins: execution state, retries, memory, approvals, audit history, chat channels, model routing, credentials, and safe side effects.

That glue code becomes the product. Frameworks help you build demos quickly, but they usually stop before the operational layer: who can run what, what happened, what changed, what needs approval, and how the system recovers.

Open Agency exists for that gap. It is the backend for agents that need to behave like production software: inspectable, governable, durable, and usable from a browser, API, CLI, or chat channel.

## Visual Architecture

```text
                     Browser UI / CLI / API / Chat Channels
                                Discord / Telegram / Slack
                                      |
                                      v
+--------------------------------------------------------------------------------+
|                                 Open Agency                                    |
|                                                                                |
|  +-------------------+    +-------------------+    +------------------------+  |
|  | Setup + Admin     |    | Conversations     |    | Workflows + Runtime    |  |
|  | Local onboarding  |--->| Main agent entry  |--->| Events / approvals     |  |
|  +-------------------+    +-------------------+    +------------------------+  |
|            |                        |                           |              |
|            v                        v                           v              |
|  +-------------------+    +-------------------+    +------------------------+  |
|  | Model Profiles    |    | Memory + Docs     |    | Tools / MCP / A2A      |  |
|  | Local + cloud LLM |    | Personas + graph  |    | Safe side effects      |  |
|  +-------------------+    +-------------------+    +------------------------+  |
|                                                                                |
+--------------------------------------+-----------------------------------------+
                                       |
                                       v
             Postgres · Redis · Docker Workers · LLM Providers · Integrations
```

## What It Does

### Launch a usable agent backend quickly

- Start the stack with one launcher command.
- Open first-run users into `/setup` instead of developer internals.
- Create a local admin, connect an LLM, and finish main-agent setup through a guided flow.

### Run agents with operational control

- Start, observe, approve, cancel, and inspect workflow executions.
- Persist runtime events, artifacts, status, approvals, and failure state.
- Keep schedules and recurring work inside the same control plane.

### Give agents durable context

- Store memory records, documents, summaries, personas, and shared workflow recall.
- Preserve context beyond a single prompt or chat turn.
- Project graph-backed context for debugging, lineage, and agent handoff.

### Keep tools and side effects governed

- Register tools with identities, schemas, permissions, and approval policies.
- Support Open Agency-native tools plus MCP and A2A integration boundaries.
- Retrieve content or operate the same retained, owner-scoped browser session through the unified browser tool family.
- Use Docker-backed runtime isolation where stronger execution boundaries are needed.

### Reach users through multiple surfaces

- Power browser, API, and CLI flows from the same backend.
- Reuse the same conversation/runtime engine across chat adapters.
- Keep Discord, Telegram, WhatsApp, Slack, and Teams as adapters rather than separate products.

## Why It Exists

**The hard part of agent systems is not text generation. It is operations.**

Open Agency is built around a few design principles:

- The backend is the product boundary. Agents, tools, workflows, memory, models, and schedules should be governed through stable APIs.
- Runtime behavior must be inspectable. Every execution should have events, artifacts, status, and audit history.
- Tools need contracts. Side effects should be typed, discoverable, permission, and approval-aware.
- Memory should survive the prompt. Durable recall cannot live only in transient context windows.
- Integrations should stay open. Local models, cloud models, MCP servers, A2A agents, and custom tools should fit the same system.

## Quick Start

This repository is the Open Agency backend. Its companion frontend is
[`open-agency-fe`](https://github.com/knivore/open-agency-fe), expected as a sibling checkout by
the local launchers. The `AGENCY_*` environment variables, `x-agency-*` headers, and `./agency`
CLI name are retained as stable protocol and command identifiers.

### Mac

```bash
git clone https://github.com/knivore/open-agency.git
cd open-agency
bash install/install-mac.sh
```

Then follow the browser setup flow:

```text
/setup -> create local admin -> connect model -> finish main agent
```

### Windows

Run from PowerShell:

```powershell
git clone https://github.com/knivore/open-agency.git
cd open-agency
powershell -ExecutionPolicy Bypass -File .\install\install-windows.ps1
```

Then follow the browser setup flow:

```text
/setup -> create local admin -> connect model -> finish main agent
```

### Linux / WSL

```bash
git clone https://github.com/knivore/open-agency.git
cd open-agency
bash install/install-linux.sh
```

For WSL, install Docker Desktop for Windows first and enable WSL integration for your Linux distro.

### Existing Checkout

If you already have the repo:

```bash
./agency start
```

Start later on Mac or Linux:

```bash
cd ~/OpenAgency/open-agency
./agency start
```

Start later on Windows:

```powershell
cd $HOME\OpenAgency\open-agency
.\run-windows.cmd start
```

The Windows launcher streams startup progress and automatically chooses how to run the sibling frontend. It uses the
native Node.js checkout when writable; in restricted workspaces it mounts the frontend source read-only in Docker and
keeps `node_modules` and `.next` in managed volumes. No ACL change or elevation is required. Set
`AGENCY_FRONTEND_RUNTIME=native` or `container` only when you need to override the default `auto` selection.

Useful commands:

```bash
./agency doctor
./agency bootstrap
./agency start
./agency logs
./agency stop
./agency status
```

Prerequisites:

- Docker Desktop or Docker Engine
- Python 3.12+
- Node.js/npm only when forcing the native Windows frontend runtime; the automatic Docker fallback includes Node.js

Installer options:

```bash
bash install/install-mac.sh --no-start
bash install/install-linux.sh --backend-only
bash install/install-mac.sh --ngrok
bash install/install-linux.sh --cloudflare
```

```powershell
powershell -ExecutionPolicy Bypass -File .\install\install-windows.ps1 -NoStart
powershell -ExecutionPolicy Bypass -File .\install\install-windows.ps1 -BackendOnly
powershell -ExecutionPolicy Bypass -File .\install\install-windows.ps1 -TunnelProvider ngrok
```

Tunnel behavior:

- First launch tries a public tunnel by default.
- Use `-local` when you want local-only startup.
- Use `--domain agency.example.com` with `-ngrok` or `-cloudflare` when you already own a reserved provider hostname.
- The setup UI can save local-only, ngrok, Cloudflare, or automatic mode.
- A browser-saved tunnel preference becomes the default on later launches.
- Custom domains can be stored for ngrok or Cloudflare-managed tunnel setups.
- Selecting ngrok or Cloudflare in the setup UI saves the preference; the next restart automatically installs a missing selected provider through WinGet on Windows or Homebrew on macOS. Fresh starts also attempt the default Cloudflare provider. For ngrok, set `AGENCY_NGROK_AUTHTOKEN` when required (interactive startup can prompt once). Cloudflare quick tunnels need no token; managed/custom Cloudflare tunnels require `AGENCY_CLOUDFLARE_TUNNEL_TOKEN` and a public URL. If an executable is not on `PATH`, set `AGENCY_NGROK_BIN` or `AGENCY_CLOUDFLARE_TUNNEL_BIN` to its verified path. Set `AGENCY_TUNNEL_AUTO_INSTALL=false` to disable automatic installation.

## Example Usage

Start Open Agency:

```bash
./agency start
```

Complete onboarding in the browser. On first run, the launcher points you to `/setup`; after setup, it points you to the main product surface.

Inspect the backend directly:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/conversations/main-agent-profile | jq
curl http://localhost:8000/tools | jq
```

At that point you have:

- a database-backed main agent
- durable memory infrastructure
- governed tool access
- runtime execution APIs
- setup-managed model configuration
- a backend reusable from an app, CLI, or chat adapter

## Key Features

### Simple Installer

Get from a clean machine to a running local system without hand-wiring the stack.

The Mac, Windows, and Linux installers clone the project, check prerequisites, bootstrap dependencies, run health checks, and hand off to the same launcher/onboarding flow.

### Browser Onboarding

Turn first-run setup into a guided product flow.

The `/setup` surface handles local admin creation, model profile setup, main-agent bootstrap, recommended supporting agents, and tunnel preference management.

### Native Runtime

Run agents and workflows with lifecycle control.

Open Agency records events, artifacts, approvals, failures, and status so operators can see what happened instead of guessing from logs.

### Durable Memory

Give agents context that survives a prompt.

Memory records, document ingestion, summaries, graph context, and workflow shared memory let agents build continuity over time.

### Governed Tools

Make side effects safe enough to operate.

Tools have stable identities, schemas, permissions, labels, and approval policies, with support for generated tools and runtime execution boundaries.

### Multi-Channel Conversations

Use one agent backend across many user surfaces.

Chat adapters can connect to the same conversation, memory, approval, and runtime systems instead of creating separate bot backends.

### Persona Factory

Turn source material into reusable agent expertise.

Personas package knowledge, style, guardrails, and provenance, then publish into runtime agents through a governed review flow.

### Open Integration Boundaries

Connect Open Agency to the broader agent ecosystem.

MCP and A2A support let Open Agency expose and consume capabilities without giving up its own runtime controls.

## How It Works

Open Agency is organized into four layers:

1. API and catalog
   FastAPI routes manage agents, tools, workflows, schedules, model profiles, memory, and executions.

2. Runtime and orchestration
   The native runtime executes workflows, enforces policy, records events, manages approvals, and coordinates workers.

3. Knowledge and identity
   Memory, documents, graph projection, and personas provide durable context for agents and operators.

4. Integration boundary
   Model providers, channel adapters, MCP servers, A2A endpoints, optional modules, and generated tools connect to one backend.

Core backend areas:

- `app/api`
- `app/domain`
- `app/db`
- `app/services`
- `app/runtime`
- `app/tools`
- `app/protocols`
- `app/observability`

Related frontend:

- `open-agency-fe` is the sibling Next.js UI used for onboarding, workflows, conversations, integrations, and optional-module surfaces.

## Comparison

| Approach              | Good for            | Where it breaks                                                | Why Open Agency is different                            |
|-----------------------|---------------------|----------------------------------------------------------------|---------------------------------------------------------|
| Agent framework       | Fast prototypes     | Ops, memory, approvals, audit, and channels become custom glue | Open Agency starts at the backend layer the demo skips  |
| Internal glue stack   | Full control        | Expensive to standardize and hard to maintain                  | Open Agency gives the shared control plane upfront      |
| Bot-specific backend  | One chat surface    | Logic gets copied across every channel                         | Open Agency keeps channels as adapters over one runtime |
| Workflow automation   | Deterministic jobs  | Weak agent identity, memory, and planning semantics            | Open Agency is designed for agentic execution           |
| Hosted agent platform | Managed convenience | Less control over runtime, memory, tools, and local data       | Open Agency is open and local-first                     |

## Real Use Cases

- Build an internal main agent that can answer questions, use tools, request approvals, and remember project state.
- Run recurring agent workflows with durable history, artifacts, and operator visibility.
- Add a Discord or Telegram assistant backed by the same runtime your app uses.
- Give workflow agents shared project memory instead of forcing every run to start cold.
- Publish source-backed personas for support, research, operations, or domain-specific assistants.
- Expose Open Agency-managed capabilities through MCP or A2A for interoperability.
- Connect optional capability packs while keeping orchestration behind one backend contract.

## Roadmap

- Clean-machine installer validation across macOS, Windows, Linux, and WSL.
- Signed and versioned release distribution with update and rollback behavior.
- Richer setup UX for model routing, supporting agents, and optional integrations.
- Deeper graph-backed runtime context and debugging flows.
- Stronger generated-tool packaging and discovery.
- Expanded persona review, governance, and publishing loops.
- Continued hardening of multi-channel operations and observability.

## Contributing

Open Agency is a long-lived backend. Contributions should preserve clear module boundaries and avoid parallel systems.

Start here:

```bash
./agency bootstrap
make test
make check-architecture
make check-tool-registry
```

Guidelines:

- Keep route handlers thin and move behavior into services.
- Treat `app/domain` contracts as canonical.
- Keep add-on feature work behind optional module specs instead of hardcoding pack-specific routes, tools, or settings into core.
- Document non-obvious orchestration, approval, retry, adapter, or guardrail logic close to the code.
- Open an issue first for larger changes or feature proposals.

If you use Open Agency as a foundation for your own product, deployment, fork, or internal platform, please retain attribution
to the original project and consider opening pull requests for fixes, hardening work, documentation improvements, or
general-purpose features that could help the upstream project.

Contributor docs:

- [Contributing](./CONTRIBUTING.md)
- [Code of Conduct](./CODE_OF_CONDUCT.md)
- [Security Policy](./SECURITY.md)
- [Architecture](./docs/architecture.md)
- [Development](./docs/development.md)
- [Testing](./docs/testing.md)
- [Runtime](./docs/runtime.md)
- [Tools](./docs/tools.md)
- [Unified Browser](./docs/unified-browser.md)
- [Memory](./docs/memory.md)
- [Persona Factory](./docs/persona-factory.md)
- [Platform Runbook](./docs/runbook.md)

## Author and Attribution

Open Agency was created by Keh Chin Leong (KEH) and is maintained with contributions from the Open Agency community.

When redistributing or building on this repository, please credit the original Open Agency project and preserve applicable
copyright, license, and attribution notices.

## License

Licensed under the [Apache License 2.0](./LICENSE).

Copyright 2026 Open Agency contributors.
