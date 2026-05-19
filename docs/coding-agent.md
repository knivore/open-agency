# Coding Agent

This guide is the canonical reference for Agency's Codex-based coding workflow.

It covers two layers:

- The `Coder` agent setup used in the frontend and backend agent registry.
- The `app.coding_agent.*` runtime used to prepare, run, and review local Codex jobs without exposing unrestricted shell
  access to frontend or Telegram users.

## Coder Agent Setup

The `Coder` agent can satisfy the runtime-agent checklist when all of these are true:

- The agent uses an OpenAI Codex-capable model profile.
- The agent has `agency.command.run` assigned.
- The command tool supports long-running commands through `timeout_seconds`.
- The user approves command-tool executions when requested.
- Codex CLI is installed and authenticated in the runtime that will invoke it: the host for `./run.sh start`,
  or the backend/worker container for fully Dockerized runs.

If `agency.command.run` is not assigned, the agent can explain and plan, but it cannot execute Codex, inspect repos,
capture diffs, or run tests.

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

Docker workspace defaults:

- Backend: `/workspace/agency`
- Frontend: `/workspace/agency-fe`

Override the frontend host mount with `AGENCY_FE_DIR` when the frontend repo is not at `../agency-fe`.

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

If no canonical tools appear in the frontend, run backend startup/tool sync first. For normal first-run setup, use:

```bash
make setup-agents
```

To update only the Coder agent:

```bash
.venv/bin/python scripts/setup.py coder-agent
```

`python scripts/setup.py` provisions this agent together with the main, Embedding, and Evaluation agents.

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
- Backend: use alias `agency`, `agency-backend`, or `backend`. In Docker this resolves to `/workspace/agency`.
- Frontend: use alias `agency-fe`, `agency-frontend`, or `frontend`. In Docker this resolves to `/workspace/agency-fe`.

Use only the selected or clearly inferred workspace. Do not access credential folders or secret files, including `.env`, `~/.ssh`, `~/.aws`, `~/.codex`, or `~/.config`.

## Workflow

1. Identify the target workspace and restate the concrete coding objective.
2. Inspect first with read-only commands such as `pwd`, `git status --short`, `rg`, `ls`, and focused file reads.
3. Create a task markdown file under `var/coding_jobs/<job-id>/task.md` when invoking Codex CLI. Include objective, workspace, constraints, expected deliverables, suggested checks, and original user request.
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

## Implemented Pieces

- `app/coding_agent/jobs.py` creates filesystem-backed job folders and structured `task.md` files.
- `app/coding_agent/workspaces.py` resolves workspace aliases, allows existing local workspace paths, and blocks
  credential-sensitive paths.
- `app/coding_agent/codex_runner.py` invokes `codex exec --sandbox workspace-write` with stdout/stderr capture.
- `app/coding_agent/git_tools.py` captures `git status --short` and `git diff --`.
- `app/coding_agent/test_runner.py` runs detected npm or Python checks with timeouts.

## Workspace Matrix

| Input | Resolution | Allowed | Notes |
| --- | --- | --- | --- |
| `agency`, `agency-backend`, `backend` | `AGENCY_BACKEND_WORKSPACE` or this repo root | Yes | Backend repo alias. Docker default: `/workspace/agency`. |
| `agency-fe`, `agency-frontend`, `frontend` | `AGENCY_FRONTEND_WORKSPACE` or sibling `agency-fe` directory | Yes | Frontend repo alias. Docker default: `/workspace/agency-fe`. |
| Existing absolute or relative directory | Resolved with `Path(...).expanduser().resolve()` | Yes | Allowed if it exists and is not credential-sensitive. |
| Missing directory | Resolved path | No | Rejected before Codex or tests run. |
| Path containing `..` | N/A | No | Rejected before resolution to avoid traversal ambiguity. |
| Credential-sensitive directory | N/A | No | Paths containing `.ssh`, `.aws`, `.codex`, `.config`, `.docker`, `.gnupg`, or `.kube` are rejected. |
| `.env` file path | N/A | No | Task files named `.env` are rejected. |

Default aliases:

- `AGENCY_BACKEND_WORKSPACE`, falling back to this repository root.
- `AGENCY_FRONTEND_WORKSPACE`, falling back to a sibling `agency-fe` directory.

## Actor And Capability Matrix

| Actor or tool | Workspace scope | Can read | Can modify | Can delete | Approval behavior |
| --- | --- | --- | --- | --- | --- |
| Main agent direct chat | Backend APIs and assigned tools | Yes, via assigned tools | Yes, if assigned mutation tools and policy allows | Only through assigned tools | Mutation tools and command tool are approval-gated where marked. |
| `agency.command.run` | Current command working directory | Yes | Yes | Yes, but risky forms are gated or blocked | All command execution requires approval. Broad deletion and credential patterns are hard-blocked. |
| Coding-agent Codex runner | Any existing non-blocked local workspace | Yes | Yes, inside Codex sandbox | Prompt-prohibited unless explicitly approved | Current enforcement is prompt-level for Codex file deletes; OS-level pre-delete approval is future work. |
| Git capture tools | Resolved workspace | `git status`, `git diff` | No | No | Read-only; mutating git commands are blocked in `git_tools.py`. |
| Test runner | Resolved workspace | Yes | Test commands may write build/cache artifacts | No explicit delete permission | Runs detected npm/Python checks with timeouts. |
| Manual script | Provided workspace | Yes | Yes via Codex CLI | Prompt-prohibited unless explicitly approved | Same limitations as Codex runner. |

## Command Policy Matrix

| Command class | Examples | Default behavior | Reason |
| --- | --- | --- | --- |
| Safe read-only inspection | `pwd`, `ls`, `cat non-secret`, `grep`, `sed`, `git status`, `git diff`, `git log` | Approval required by `agency.command.run`; allowed after approval | Useful for debugging and repository inspection. |
| Build and tests | `npm test`, `npm run build`, `npm run lint`, `pytest`, `python -m pytest` | Approval required by `agency.command.run`; allowed after approval | May execute project code but is expected in development workflows. |
| Package or environment mutation | `npm install`, `pip install`, `brew install`, `docker compose up`, `alembic upgrade` | Approval required; not hard-blocked by current executor | Can change local environment or data, so user approval is required. |
| Normal file writes | `tee file`, `python script_that_writes.py`, `touch file` | Approval required; allowed after approval | Enables coding workflows. |
| Targeted deletion | `rm path/to/file`, `rm -r local/generated-dir` | Approval required; allowed after approval unless it matches a hard block | User asked deletion to be permission-gated, not categorically impossible. |
| Broad deletion | `rm -rf /`, `rm -rf $HOME`, `rm -rf ~` | Hard-blocked | Too destructive to allow through generic agent commands. |
| Find deletion | `find . -delete` | Hard-blocked | Too easy to delete broad trees unintentionally. |
| Privilege escalation | `sudo`, `su` | Hard-blocked | Breaks local trust boundary. |
| Remote shell/file transfer | `ssh`, `scp` | Hard-blocked | Can expose credentials or affect remote systems. |
| Remote install pipe | `curl ... | bash`, `wget ... | sh` | Hard-blocked | High-risk supply-chain pattern. |
| Git push | `git push` | Hard-blocked | Publishing requires a separate explicit workflow. |
| Credential reads | `cat ~/.ssh/*`, `cat ~/.aws/*`, `cat .env` | Hard-blocked | Secrets must not be exposed to agent context. |

## Delete Permission Matrix

| Surface | Targeted delete | Broad delete | Current enforcement |
| --- | --- | --- | --- |
| `agency.command.run` | Requires approval, then allowed unless blocked | Hard-blocked for root/home recursive patterns and `find -delete` | Enforced before subprocess execution. |
| Codex runner | Prompt says deletion requires explicit approval | Prompt says destructive commands are not allowed | Prompt-level only; needs future workspace runner for pre-delete interception. |
| Git tools | Not supported | Not supported | Mutating git commands are blocked. |
| Test runner | Not intended | Not intended | Runs command lists directly; default checks do not include delete commands. |

## Job Artifact Matrix

| Artifact | Path inside job folder | Producer | Purpose |
| --- | --- | --- | --- |
| Task markdown | `task.md` | `create_coding_job` | Structured task for Codex. |
| Job metadata | `job.json` | `create_coding_job` | Durable job record. |
| Codex stdout | `codex_stdout.log` | Future orchestration layer | Captured Codex output. |
| Codex stderr | `codex_stderr.log` | Future orchestration layer | Captured Codex errors. |
| Git status | `git_status.txt` | Future orchestration layer using `get_git_status` | Changed-file summary. |
| Git diff | `git_diff.patch` | Future orchestration layer using `get_git_diff` | Reviewable patch. |
| Test output | `test_output.log` | Future orchestration layer using `run_default_checks` | Build/test result log. |
| Summary | `summary.md` | Future orchestration layer | Human-facing completion summary. |

## Current Enforcement Gaps

| Gap | Current behavior | Required next step |
| --- | --- | --- |
| Codex pre-delete approval | Prompt-level instruction only | Add an approval-aware workspace runner, filesystem monitor, or isolated worktree diff gate. |
| API approval workflow | Not wired | Add backend coding-job routes and approval records before execution/commit. |
| Frontend review UI | Not wired | Add coding-agent dashboard with task/log/diff/test panels. |
| Commit flow | Not wired | Add explicit approve-commit endpoint; keep push disabled by default. |
| Container isolation | Not wired for coding-agent jobs | Add Docker/devcontainer or disposable worktree isolation for high-risk automation. |

## Current Non-Goals

- no API routes yet
- no frontend dashboard yet
- no automatic commits or pushes
- no unrestricted shell exposure to external channels
