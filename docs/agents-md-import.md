# Agent Markdown Import

Agency can import Markdown-authored agent files from Claude Code, OpenCode, Copilot, Antigravity-style agents,
`SKILL.md` files, and plain Markdown prompts. The importer is deterministic in v1: it parses frontmatter and Markdown
without calling an LLM, returns a reviewable proposal, and only saves an agent when the caller commits the proposal.

## Supported Sources

- Multipart upload of one `.md` file through `POST /agents/import/preview`.
- Multipart upload of multiple `.md` files through `POST /agents/import/batch-preview`.
- Raw Markdown JSON through `markdown_text`.
- Remote HTTPS URL through `source_url`.
- GitHub `blob` URLs are normalized to `raw.githubusercontent.com` before fetching Markdown content.
- Local backend-only CLI paths with `python -m app.cli agent import-preview`, `import-commit`, or `import-batch`.

Remote URLs are restricted to `https`, do not follow redirects, must resolve to public network addresses, and must return
text or Markdown-like content within the importer size limit.

Preview and commit operations emit execution audit events. The importer creates a hidden internal workflow named
`agent-markdown-import` on demand so database-backed execution records satisfy workflow foreign-key constraints without
requiring manual seed data.

## Markdown Format

Minimal Markdown works:

```markdown
# Frontend Developer

You are a frontend specialist focused on React, accessibility, and implementation quality.
```

Frontmatter gives the importer stronger fields:

```markdown
---
name: Frontend Developer
description: Expert frontend developer specializing in React and accessibility.
role: Modern web application and UI implementation specialist
model_profile_id: openai-default
tool_ids:
  - agency.graph.context
handoff_agent_ids:
  - backend-architect
color: cyan
tags:
  - frontend
  - accessibility
---
# Frontend Developer Agent Personality

You are **Frontend Developer**, an expert frontend developer.
```

Mapping rules:

- `name`, `display_name`, `description`, `role`, `backstory`, and valid `model_profile_id` map onto `AgentDefinition`.
- The Markdown body maps to both `instructions` and `system_prompt`.
- If `name` is missing, the first H1 is used as the name.
- Unknown frontmatter is preserved under `metadata.import.frontmatter`.
- Style metadata such as `color`, `emoji`, `vibe`, and `tags` is copied into `framework_hints.metadata`.
- Body sections with headings such as `Tools`, `Capabilities`, `Handoffs`, or `Specialist Agents` are scanned for
  exact existing Agency tool and agent names. Matches become suggestions only; they are not assigned unless approved.

Format detection is deterministic and conservative:

- `agency_agents_markdown`: Agency-style frontmatter with `name`, `description`, and style metadata such as `color`,
  `emoji`, or `vibe`.
- `skill_md`: filename `SKILL.md` or skill-like Markdown containing skill and trigger sections.
- `claude`: explicit Claude/Claude Code provider metadata, or Claude-style subagent frontmatter with `name`,
  `description`, and `tools`.
- `copilot`: explicit Copilot metadata, `applyTo` frontmatter, `copilot-instructions.md`, or `.instructions.md`
  filenames.
- `opencode`: explicit OpenCode provider metadata.
- `antigravity`: explicit Antigravity provider metadata.
- `generic_markdown`: fallback for any importable Markdown without stable provider markers.

## API

Supported formats:

```http
GET /agents/import/formats
```

The response lists deterministic format ids, supported import modes, commit strategies, and whether LLM normalization is
available.

Preview one source:

```http
POST /agents/import/preview
Content-Type: application/json

{
  "markdown_text": "# Agent\n\nInstructions...",
  "source_filename": "agent.md",
  "use_llm_normalization": false
}
```

Preview responses return an `AgentImportProposal`:

- `source`: source type, filename or URL, and source SHA-256.
- `detected_format`: one of the deterministic format ids.
- `agent`: proposed `AgentDefinition` with instructions, metadata, framework hints, and disabled-by-default metadata.
- `suggested_tool_ids`: existing or missing tool suggestions, all review-required.
- `suggested_handoff_agent_ids`: existing or missing handoff suggestions, all review-required.
- `warnings`: structured warning codes, messages, severity, and optional field.
- `conflicts`: existing agent id or normalized-name conflicts.
- `requires_review`: always `true` for v1 imports.

Commit one proposal:

```http
POST /agents/import/commit
Content-Type: application/json

{
  "proposal": { "...": "preview response" },
  "conflict_strategy": "create_only",
  "approved_tool_ids": [],
  "approved_handoff_agent_ids": [],
  "model_profile_id": null,
  "enabled": false
}
```

Batch preview JSON:

```http
POST /agents/import/batch-preview
Content-Type: application/json

{
  "items": [
    {"markdown_text": "# Frontend\n\nInstructions...", "source_filename": "frontend.md"},
    {"markdown_text": "# Backend\n\nInstructions...", "source_filename": "backend.md"}
  ]
}
```

Batch upload uses multipart form data with repeated `files` fields. Batch commit accepts `items`, where each item is the
same shape as a single commit request. Batch operations return successful proposals/results and per-item errors so one
bad file does not block the rest of the batch.

Conflict strategies:

- `create_only`: fail if an existing agent matches by id or normalized name.
- `update_existing`: update the matching existing agent while preserving existing tools and model profile unless the
  request explicitly changes them.
- `duplicate_as_new`: create a new agent id and deduplicate the name.

Common structured error codes:

- `empty_markdown`
- `empty_markdown_body`
- `invalid_frontmatter`
- `markdown_too_large`
- `instructions_too_large`
- `source_url_blocked`
- `source_url_fetch_failed`
- `source_content_type_invalid`
- `source_encoding_invalid`
- `agent_import_conflict`
- `tool_not_found`
- `handoff_agent_not_found`
- `model_profile_not_found`
- `llm_normalization_model_profile_required`
- `llm_normalization_model_profile_not_found`
- `llm_normalization_unavailable`

## Security Model

Imported Markdown is untrusted source content.

- Imported agents default to disabled unless the commit request enables them.
- Tool IDs in Markdown are suggestions only; no tool is granted unless the caller passes it in `approved_tool_ids`.
- Handoff IDs are suggestions only; no handoff is assigned unless the caller passes it in `approved_handoff_agent_ids`.
- Unknown tools and missing handoff agents are returned as warnings.
- High-risk tools are marked review-required.
- LLM normalization is explicitly unavailable in v1.

The importer stores provenance under `metadata.import`, including source type, filename, URL when present, SHA-256 hash,
detected format, importer user, and review status.

Safety scanning is deterministic and warning-only. Preview responses may include:

- `prompt_injection_detected` when instructions appear to override policies, approvals, or higher-priority messages.
- `secret_like_value_detected` when API-key, token, password, or credential-shaped text is present.
- `tool_grant_instruction_detected` when source text asks for automatic tool or permission grants.
- `shell_snippet_detected` when source text includes executable shell snippets.

These warnings do not rewrite imported Markdown. They make review risk explicit while preserving the source text for the
human reviewer.

## Audit Events

Preview and commit operations emit execution-store audit events:

- `agent.import.previewed`
- `agent.import.committed`

The preview response includes `metadata.import.preview_audit_execution_id`. A commit made from that proposal reuses the
same audit execution and stores `metadata.import.commit_audit_execution_id` on the saved agent.

Audit payloads are redacted by construction. They include source type, filename, URL, SHA-256 hash, detected format,
agent id/name, warning codes, conflict summaries, commit strategy, and approved tool/handoff IDs. They do not include raw
Markdown instructions or frontmatter values that could duplicate secrets.

## CLI

Preview one file:

```bash
python -m app.cli agent import-preview path/to/agent.md
```

Commit one file:

```bash
python -m app.cli agent import-commit path/to/agent.md --conflict-strategy create_only
```

Batch preview is dry-run by default:

```bash
python -m app.cli agent import-batch path/to/agents --recursive
```

Batch commit:

```bash
python -m app.cli agent import-batch path/to/agents --recursive --commit --conflict-strategy update_existing
```

Useful commit flags:

- `--approve-tool TOOL_ID`
- `--approve-handoff AGENT_ID_OR_NAME`
- `--model-profile-id PROFILE_ID`
- `--enabled`
- `--json`

## LLM Normalization Contract

The API accepts `use_llm_normalization` and `llm_normalization_model_profile_id`, but v1 does not invoke a model. When
normalization is requested:

- Missing `llm_normalization_model_profile_id` returns `llm_normalization_model_profile_required`.
- Unknown model profiles return `llm_normalization_model_profile_not_found`.
- A valid model profile records the request and returns `llm_normalization_unavailable`.

The future model output contract is strict: normalized agent fields, suggested tool mappings with confidence/rationale,
suggested handoff mappings with confidence/rationale, warnings, and assumptions. Tool and handoff suggestions will remain
review-only after model output validation.

## Deferred Work

The v1 importer intentionally leaves these items for later implementation:

- GitHub repository or folder import with `repository_url` and `path_glob`.
- Persisted import draft records with expiry, if stateless preview becomes insufficient.
- Runtime configuration settings for LLM normalization availability.
- Actual LLM normalization with a policy prompt, strict output validation, model-generated warning preservation, and
  tests proving model-suggested tools/handoffs remain review-only.
- Permission checks beyond `agents:read`/`agents:write`, especially tool-management permission for approving tool grants
  and elevated permission for remote URL import.
- Workflow-template proposals for orchestrator imports.
- Explicit unsupported-file-type UX in the frontend.
- Commit-time tests for real LLM-normalized output once model invocation is implemented.
