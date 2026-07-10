# Coding Agent

This guide is the canonical reference for Agency's Codex-based coding workflow.

The supported path is a `Coder` agent in the frontend/backend agent registry using the approval-gated
`agency.command.run` tool. The `Coder` agent should prepare, run, review, and summarize local Codex work through that
tool instead of relying on a separate backend coding-job package.

## Coder Agent Setup

The `Coder` agent can satisfy the runtime-agent checklist when all of these are true:

- The agent uses an OpenAI Codex-capable model profile.
- The agent has `agency.command.run` assigned.
- The command tool supports long-running commands through `timeout_seconds`.
- The user approves command-tool executions when requested.
- The agent adds concise inline comments, docstrings, or function-level notes for non-obvious implementation reasoning.
- Codex CLI is installed and authenticated in the runtime that will invoke it: the host for `./run.sh start`,
  or the backend/worker container for fully Dockerized runs.

If `agency.command.run` is not assigned, the agent can explain and plan, but it cannot execute Codex, inspect repos,
capture diffs, or run tests.

## Runtime Timeout Policy

Agency resolves timeout configuration into one per-execution runtime policy and stores it on
`execution.metadata.runtime_policy`. The policy controls the isolated worker hard timeout, stale-run monitoring,
Codex CLI timeout, and LLM request timeout. The global defaults come from `AGENT_RUN_TIMEOUT_SECONDS`,
`AGENT_ACTIVITY_IDLE_TIMEOUT_SECONDS`, `CODEX_CLI_TIMEOUT_SECONDS`, and `LLM_REQUEST_TIMEOUT_SECONDS`.

Long-running coding agents can override those defaults through workflow, task, or agent metadata:

```json
{
  "timeout_policy": {
    "idle_timeout_seconds": 1200,
    "run_timeout_seconds": 14400,
    "codex_cli_timeout_seconds": 3600,
    "llm_request_timeout_seconds": 90
  }
}
```

Use `runtime_policy` for new metadata and `timeout_policy` for compatibility with existing definitions. Isolated worker
startup resolves the longest task/agent timeout in the workflow so a coding agent with a longer policy receives a longer
worker lifetime before the first task activity event exists.

Optional Agency Graph context:

- `scripts/setup.py coder-agent` assigns `agency.graph.context` when `AGENCY_GRAPH_CONTEXT_TOOLS_ENABLED=true`.
- Keep `agency.graph.context` assigned only for coding workflows that benefit from read-only graph history.
- Query graph context before resuming nontrivial prior work, debugging repeated failures, or taking over a handoff.
- Prefer intents `resume`, `debug`, or `learn`, `budget=balanced`, and raw graph output disabled by default.
- Start from `anchor_type=task`, `workflow`, `run`, `execution`, `conversation`, or a query about the repository or working tree
  when the exact anchor is unknown.
- Inspect decisions, constraints, prior attempts, failures, and next actions before running commands so the agent does
  not repeat known failed approaches.
- If the graph is disabled, unavailable, or empty, fall back to durable memory, execution events, `git log`, and focused
  repository inspection.

## Host Backend Runtime

For local Codex-first chat, prefer running the backend/main-agent process on the host:

```bash
./run.sh start
```

In this mode, main-agent chat uses the host Codex CLI and host `~/.codex`. Workflow and tool executions still run in
Docker worker containers when `EXECUTION_ISOLATION_ENABLED=true`.

Working-directory split:

- `CODEX_CLI_CWD`: host-visible cwd for main-agent/Codex chat calls
- `EXECUTION_CODEX_CLI_CWD`: container-visible cwd for isolated worker executions, usually `/app`

The script syncs host Codex OAuth into the persistent Docker Codex volume so isolated workers can also use Codex when a
workflow calls for it.

## Docker Backend Runtime

The backend Docker image installs the Codex CLI with npm during build. The Compose service also sets `CODEX_HOME=/codex`
and stores that directory in the persistent `codex_home` volume, so container-side Codex login survives rebuilds. This is
the compatibility path for a fully Dockerized backend:

After Dockerfile or Compose changes, rebuild and restart the backend:

```bash
docker compose up --build --force-recreate -d backend
```

Verify the CLI is available inside the backend container:

```bash
docker compose exec backend codex --version
```

Authenticate Codex inside the container only if you are running the backend fully inside Docker and do not sync host
Codex OAuth into the volume:

```bash
docker compose exec backend codex login
```

For API-key mode, set `OPENAI_API_KEY` in the host environment or `.env`; Compose passes it into the backend container
when using the Docker backend.
For device-code auth, which is usually easier from a headless container:

```bash
docker compose exec backend codex login --device-auth
```

Check whether the backend container can see a Codex login:

```bash
docker compose exec backend codex login status
```

If you have already logged into Codex on the host and want Docker to reuse that auth, set `CODEX_HOME_SOURCE` to a copy
of the host Codex home before starting Compose. On Windows, Codex usually stores this at `%USERPROFILE%\.codex`.

```powershell
$env:CODEX_HOME_SOURCE = "C:/Users/chink/.codex"
docker compose up --build --force-recreate -d backend
docker compose exec backend codex login status
```

Mounting the host Codex home gives the backend container access to the same `auth.json` used by the host CLI. Treat that
directory like credentials.

For API-key mode, set `OPENAI_API_KEY` in the host environment or `.env`; Compose passes it into the backend container.

Docker workspace defaults:

- Backend: `AGENCY_BACKEND_WORKSPACE`
- Frontend: `AGENCY_FRONTEND_WORKSPACE`

These trusted local development workspace mounts are read-write. Isolated workflow containers use the host paths from
`AGENCY_BACKEND_HOST_WORKSPACE` and `AGENCY_FRONTEND_HOST_WORKSPACE`; when those paths or their mounted targets are
visible to the backend, Agency probes them before launch and fails early with an operator-facing permission prompt if
the current user cannot write. That prompt means the human should approve/fix host filesystem permissions or point the
workspace env var at a writable checkout before retrying.

Override the frontend host mount with `AGENCY_FE_DIR` when the frontend repo is not at `../open-agency-fe`. To expose another
repo to coder workflows, add it through `EXECUTION_CONTAINER_EXTRA_MOUNTS` with `"read_only": false` and set the Codex
working directory or task instructions to the matching configured container target.

## Required Tool

Assign this tool to the `Coder` agent:

```text
agency.command.run
```

This single tool is enough for the checklist because it can run:

```bash
codex exec --sandbox workspace-write ...
git status --short
git diff --
npm test
npm run build
.venv/bin/python -m unittest
```

Keep the command tool approval-gated. Do not expose unrestricted shell execution to untrusted users or external
channels.

## QA Collaboration

When a workflow asks for coding plus QA, pair the `Coder Agent` with a dedicated `QA Agent` instead of reusing a repo
reviewer that only mentions tests in its instructions. The recommended task loop is:

1. `Coder Agent` implements TODOs or selected recommendations.
2. `QA Agent` runs focused tests, lint, build, or repro checks and reports command evidence.
3. `Coder Agent` fixes any QA findings.
4. `QA Agent` rechecks the fix and gives the final pass/fail verdict.

Both agents need `agency.command.run`; handoff links should allow coder to QA and QA back to coder.

## Frontend Setup

1. Open `http://localhost:3000/agents`.
2. Create or edit an agent named `Coder`.
3. Set role to `Senior Software Engineer`.
4. Select the OpenAI Codex model profile.
5. Paste the prompt from the section below into `Instructions`.
6. Assign `agency.command.run`.
7. Save the agent.

If no canonical tools appear in the frontend, run backend startup/tool sync first or use:

```bash
.venv/bin/python scripts/setup.py coder-agent
```

Optional arguments:

```bash
.venv/bin/python scripts/setup.py coder-agent --name Coder --role "Senior Software Engineer"
```

The script updates an existing agent with the same name or creates one if missing. It preserves any existing non-command
tool assignments and ensures `agency.command.run` is assigned.

## Prompt

```markdown
# Coder Agent

You are the Agency coding runtime agent. Your job is to turn a workflow task into a safe, reviewable local code change using the tools assigned to you.

## Operating Model

Use the existing command tool, usually tool id `agency.command.run`, callable name `run_command`, display name `Run Command`, for CLI-first work. Do not invent Codex-specific APIs or dashboards. If the command tool is not assigned, stop and report: "Missing required tool: agency.command.run".

Default workspaces:
- Backend: use alias `agency`, `agency-backend`, or `backend`. In Docker this resolves to `AGENCY_BACKEND_WORKSPACE`.
- Frontend: prefer alias `open-agency-fe` or `open-agency-frontend`; legacy `agency-fe`, `agency-frontend`, and `frontend` aliases remain accepted. In Docker this resolves to `AGENCY_FRONTEND_WORKSPACE`.

Use only the selected or clearly inferred workspace. Do not access credential folders or secret files, including `.env`, `~/.ssh`, `~/.aws`, `~/.codex`, or `~/.config`.

## Code Comments

- Add concise inline comments, docstrings, or function-level notes when the purpose of a function, branch, integration boundary, domain rule, guardrail, async flow, cache, retry, or workaround is not obvious from names and structure.
- Explain why the code exists, what constraint it protects, or what reasoning justifies a non-obvious step. Do not restate what each line already says.
- Keep comments close to the code they clarify, and update or remove them when the behavior changes.
- Avoid noisy comments in straightforward code; comment the reasoning and intent future maintainers would otherwise have to rediscover.

## Workflow

1. Identify the target workspace and restate the concrete coding objective.
2. Inspect first with read-only commands such as `pwd`, `git status --short`, `rg`, `ls`, and focused file reads.
3. For non-trivial Codex CLI calls, create or reference a task markdown file under `var/coding_jobs/<job-id>/task.md`
   using the command tool. Include objective, workspace, constraints, expected deliverables, suggested checks, and
   original user request.
4. Run Codex locally with workspace sandboxing, for example:
   `codex exec --sandbox workspace-write "Read var/coding_jobs/<job-id>/task.md and implement it. Do not push to git."`
5. Use a longer `timeout_seconds` for Codex, builds, and tests when the command tool supports it.
6. After Codex finishes, always capture review artifacts:
   - `git status --short`
   - `git diff --`
   - relevant test/build output
7. Run checks appropriate to the repo. Backend usually uses `.venv/bin/python -m unittest` or targeted unittest modules. Frontend usually uses `npm test`, `npm run build`, or configured package scripts.
8. Never auto-commit or auto-push. Present the diff and test results for review.

## Safety Rules

- Do not run destructive commands unless the user explicitly requested that exact operation.
- Never run `git push`.
- Never read or print secrets.
- Keep changes small and focused.
- If tests fail, do not revert automatically. Explain the failure and, if practical, iterate once with a focused fix.
- If the workspace has unrelated user changes, do not overwrite or revert them.

## Final Response

Return a concise review summary with:
- Objective completed or blocked.
- Files changed.
- Commands/tests run and their result.
- Git diff/status artifact summary.
- Known issues or follow-ups.
```

## Current Architecture

The `Coder` agent is the coding runtime. It should use `agency.command.run` to perform the same job lifecycle a
dedicated coding-job service would have handled:

1. Resolve the target workspace from the user request or workflow context.
2. Prepare a focused task file when the Codex prompt would otherwise become too large or ambiguous.
3. Run `codex exec --sandbox workspace-write ...` from the selected workspace.
4. Capture `git status --short`, `git diff --`, and relevant test/build output.
5. Report changed files, verification results, and any unresolved issues.

Workspace aliases:

| Alias                                      | Default target                                               | Notes                |
|--------------------------------------------|--------------------------------------------------------------|----------------------|
| `open-agency`, `open-agency-backend` | `AGENCY_BACKEND_WORKSPACE` or this repo root | Canonical backend repo aliases. |
| `agency`, `agency-backend`, `backend` | Same Open Agency backend workspace | Legacy compatibility aliases. |
| `open-agency-fe`, `open-agency-frontend` | `AGENCY_FRONTEND_WORKSPACE` or sibling `open-agency-fe` directory | Canonical frontend repo aliases. |
| `agency-fe`, `agency-frontend`, `frontend` | Same Open Agency FE workspace | Legacy compatibility aliases. |

## Actor And Capability Matrix

| Actor or tool                | Workspace scope                   | Can read                | Can modify                                          | Can delete                                                | Approval behavior                                                                                 |
|------------------------------|-----------------------------------|-------------------------|-----------------------------------------------------|-----------------------------------------------------------|---------------------------------------------------------------------------------------------------|
| Main agent direct chat       | Backend APIs and assigned tools   | Yes, via assigned tools | Yes, if assigned mutation tools and policy allows   | Only through assigned tools                               | Mutation tools and command tool are approval-gated where marked.                                  |
| `agency.command.run`         | Current command working directory | Yes                     | Yes                                                 | Yes, but risky forms are gated or blocked                 | All command execution requires approval. Broad deletion and credential patterns are hard-blocked. |
| Coder agent via command tool | Selected workspace                | Yes                     | Yes, through approved commands and Codex sandboxing | Only through approved commands; broad deletion is blocked | Uses `agency.command.run` for Codex, git artifact capture, and tests.                             |

## Command Policy Matrix

| Command class                   | Examples                                                                             | Default behavior                                                         | Reason                                                                    |
|---------------------------------|--------------------------------------------------------------------------------------|--------------------------------------------------------------------------|---------------------------------------------------------------------------|
| Safe read-only inspection       | `pwd`, `ls`, `cat non-secret`, `grep`, `sed`, `git status`, `git diff`, `git log`    | Approval required by `agency.command.run`; allowed after approval        | Useful for debugging and repository inspection.                           |
| Build and tests                 | `npm test`, `npm run build`, `npm run lint`, `pytest`, `python -m pytest`            | Approval required by `agency.command.run`; allowed after approval        | May execute project code but is expected in development workflows.        |
| Package or environment mutation | `npm install`, `pip install`, `brew install`, `docker compose up`, `alembic upgrade` | Approval required; not hard-blocked by current executor                  | Can change local environment or data, so user approval is required.       |
| Normal file writes              | `tee file`, `python script_that_writes.py`, `touch file`                             | Approval required; allowed after approval                                | Enables coding workflows.                                                 |
| Targeted deletion               | `rm path/to/file`, `rm -r local/generated-dir`                                       | Approval required; allowed after approval unless it matches a hard block | User asked deletion to be permission-gated, not categorically impossible. |
| Broad deletion                  | `rm -rf /`, `rm -rf $HOME`, `rm -rf ~`                                               | Hard-blocked                                                             | Too destructive to allow through generic agent commands.                  |
| Find deletion                   | `find . -delete`                                                                     | Hard-blocked                                                             | Too easy to delete broad trees unintentionally.                           |
| Privilege escalation            | `sudo`, `su`                                                                         | Hard-blocked                                                             | Breaks local trust boundary.                                              |
| Remote shell/file transfer      | `ssh`, `scp`                                                                         | Hard-blocked                                                             | Can expose credentials or affect remote systems.                          |
| Remote install pipe             | `curl ...                                                                            | bash`, `wget ...                                                         | sh`                                                                       | Hard-blocked | High-risk supply-chain pattern. |
| Git push                        | `git push`                                                                           | Hard-blocked                                                             | Publishing requires a separate explicit workflow.                         |
| Credential reads                | `cat ~/.ssh/*`, `cat ~/.aws/*`, `cat .env`                                           | Hard-blocked                                                             | Secrets must not be exposed to agent context.                             |

## Delete Permission Matrix

| Surface                    | Targeted delete                                 | Broad delete                                                     | Current enforcement                                                                     |
|----------------------------|-------------------------------------------------|------------------------------------------------------------------|-----------------------------------------------------------------------------------------|
| `agency.command.run`       | Requires approval, then allowed unless blocked  | Hard-blocked for root/home recursive patterns and `find -delete` | Enforced before subprocess execution.                                                   |
| Codex CLI invoked by Coder | Prompt says deletion requires explicit approval | Prompt says destructive commands are not allowed                 | Codex sandboxing plus command approval; review the final diff before accepting changes. |

## Review Artifacts

For coding workflows, the `Coder` agent should produce these artifacts through normal commands:

| Artifact      | Command or source                                 | Purpose                                |
|---------------|---------------------------------------------------|----------------------------------------|
| Task markdown | `var/coding_jobs/<job-id>/task.md` when useful    | Structured task for larger Codex runs. |
| Codex output  | `codex exec --sandbox workspace-write ...` output | Implementation log and blockers.       |
| Git status    | `git status --short`                              | Changed-file summary.                  |
| Git diff      | `git diff --`                                     | Reviewable patch.                      |
| Test output   | Targeted unit, build, lint, or test command       | Verification evidence.                 |

## Current Enforcement Gaps

| Gap                       | Current behavior                                                     | Required next step                                                                                          |
|---------------------------|----------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------|
| Codex pre-delete approval | Codex prompt-level instruction plus command approval and sandboxing  | Add stronger diff gates or isolated worktrees if needed.                                                    |
| Durable coding-job UI     | Not implemented                                                      | Prefer extending the `Coder` agent workflow and frontend agent experience before adding a separate runtime. |
| Commit flow               | Not wired                                                            | Add explicit approve-commit endpoint; keep push disabled by default.                                        |
| Container isolation       | Available for workflow/tool executions depending on runtime settings | Add Docker/devcontainer or disposable worktree isolation for high-risk automation.                          |

## Current Non-Goals

- no automatic commits or pushes
- no unrestricted shell exposure to external channels
- no separate backend coding-agent runtime package
