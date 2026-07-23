# Compact Tool

## Purpose

The compact tool turns raw conversations, workflow outputs, and other information sources into small reusable context
packs. It is not a generic summarizer. Its job is to preserve the operational state another agent, workflow, or future
conversation needs in fewer tokens.

Agency should keep the full source conversation as the source of truth and store compact outputs as derived durable
memory. The compact record should point back to the original conversation, message window, workflow run, or document
source so the system can audit or regenerate it later.

## Current Agency Fit

The compact tool is implemented. The former TODO tracker has been removed; this document is the maintained behavior and
operational reference for context packs.

Agency separates raw history from reusable memory:

- raw main-agent chat history lives in `conversations` and `conversation_messages`
- durable memory lives in `memory_records`
- `daily_summary` and `run_summary` records are already stored in `memory_records`
- retrieval injects durable memory into prompt context before the model reply

The compact tool extends this model instead of creating a parallel memory store.

Persistent shape:

- keep raw conversation messages unchanged
- use the durable `context_pack` memory type
- store compact outputs in `memory_records.content`
- store a short label in `memory_records.summary`
- store structured mode output and provenance in `memory_records.metadata_json`
- embed compact records through the existing memory embedding path

## Core Principle

Do not choose between full history and compact history globally.

Use both:

- recent raw turns for local continuity
- compact context packs for older conversation state
- atomic durable memories for stable facts, preferences, decisions, and commitments
- source records for audit, replay, and regeneration

Prompt assembly should decide what to include based on token budget, model window, conversation length, and task type.

## Context Pack

A context pack is a compact reusable state object.

Example:

```json
{
  "mode": "handoff",
  "summary": "The user is designing a mode-aware conversation compaction feature for Agency.",
  "current_state": "Agency stores raw conversations separately from durable memory. The proposed tool should add compact context packs as derived memory records.",
  "decisions": [
    "Keep full conversation history as source of truth.",
    "Store compact outputs in memory_records with source provenance.",
    "Use recent raw turns plus compact older state during prompt assembly."
  ],
  "constraints": [
    "Do not mutate or delete raw conversation messages during compaction.",
    "Use existing memory scopes and access rules."
  ],
  "open_questions": [
    "Exact compaction trigger thresholds for automatic creation."
  ],
  "next_actions": [
    "Add data contract and service for context pack generation.",
    "Update prompt assembly to include compact older state when history is long."
  ]
}
```

## Modes

The tool should support mode profiles. Each mode defines what to keep, what to discard, output format, token budget, and
retrieval priority.

Recommended mode contract:

```json
{
  "id": "handoff",
  "label": "Handoff",
  "purpose": "Continue work in another chat, agent, or workflow.",
  "default_token_budget": 1200,
  "output_schema": "context_pack.handoff.v1",
  "keep": [
    "current state",
    "decisions",
    "constraints",
    "open questions",
    "next actions"
  ],
  "drop": [
    "small talk",
    "obsolete intermediate attempts",
    "duplicate explanations"
  ]
}
```

### brief

Use when a human wants a quick overview.

Keeps:

- main topic
- important outcome
- current status
- immediate next step

Drops:

- implementation details
- long rationale
- low-level chronology

Typical output:

- 100 to 300 words
- optional short bullets

### handoff

Use when another agent or future conversation must continue the work.

Keeps:

- user goal
- current state
- decisions and rationale
- constraints
- files, tools, workflows, and artifacts mentioned
- open questions
- next actions
- known pitfalls or discarded approaches

Drops:

- conversational filler
- repeated explanations
- stale plans that no longer apply

Typical output:

- compact narrative
- structured JSON
- generated handoff prompt

### memory

Use when extracting durable long-term memory.

Keeps:

- stable user preferences
- reusable project facts
- recurring constraints
- important entities
- durable commitments

Drops:

- one-off task details
- temporary debugging state
- facts likely to expire unless marked for verification

Typical output:

- candidate `fact`, `preference`, `decision`, and `task_commitment` records
- confidence and sensitivity flags
- expiration or verification notes

### workflow

Use when turning discussion into execution state.

Keeps:

- tasks
- owners or responsible agents
- statuses
- blockers
- dependencies
- expected outputs
- trigger conditions

Drops:

- background discussion not needed for execution
- duplicate task wording

Typical output:

- task list
- blocker list
- workflow input hints
- run readiness notes

### technical

Use when implementation details matter.

Keeps:

- files and modules
- APIs and schemas
- commands and test results
- technical decisions
- constraints and compatibility notes
- failure modes

Drops:

- product-level discussion unless it affects implementation
- generic explanations

Typical output:

- engineering summary
- touched files
- design decisions
- tests to run
- risks

### archive

Use when preserving a compact record of what happened.

Keeps:

- chronological outline
- key turns
- decisions
- artifacts
- outcomes

Drops:

- redundant messages
- low-value wording

Typical output:

- denser than `brief`
- less action-oriented than `handoff`
- useful for historical lookup

### custom

Use when the caller supplies explicit keep/drop rules.

Example request:

```json
{
  "mode": "custom",
  "custom_keep": ["decisions", "next_actions", "preferences"],
  "custom_drop": ["artifacts"],
  "token_budget": 800
}
```

## Storage

Context packs are durable compact records in `memory_records`.

Memory type:

```text
context_pack
```

`context_pack` is more flexible because it can represent conversation, workflow, document, or multi-source compaction.

Record shape:

```json
{
  "scope": "conversation",
  "conversation_id": "conv_123",
  "source_conversation_id": "conv_123",
  "source": "compact_tool",
  "memory_type": "context_pack",
  "status": "active",
  "importance": 65,
  "summary": "Handoff context for Agency compact tool design.",
  "content": "Rendered compact context pack...",
  "sensitive": false,
  "metadata": {
    "mode": "handoff",
    "schema_version": "context_pack.handoff.v1",
    "summary_version": "v1",
    "sensitive_source_detected": false,
    "sensitive_output_detected": false,
    "source_message_start_id": "msg_001",
    "source_message_end_id": "msg_084",
    "source_message_count": 84,
    "token_budget": 1200,
    "estimated_source_tokens": 18000,
    "estimated_compact_tokens": 1100,
    "compression_ratio": 0.061,
    "decision_refs": [],
    "open_loops": [],
    "artifact_refs": []
  },
  "tags": ["context_pack", "conversation", "handoff"]
}
```

## Generation Pipeline

Implemented pipeline:

```text
source messages
  -> normalize transcript
  -> extract neutral state
  -> rank items by selected mode
  -> compress and deduplicate
  -> render mode-specific context pack
  -> persist as durable memory
  -> embed for retrieval
```

The neutral extraction model should be shared across modes:

```json
{
  "goals": [],
  "facts": [],
  "preferences": [],
  "decisions": [],
  "constraints": [],
  "commitments": [],
  "open_questions": [],
  "next_actions": [],
  "artifacts": [],
  "risks": [],
  "discarded_approaches": [],
  "verification_needed": []
}
```

Each mode then renders a different view over the same extracted state.

## Prompt Assembly

The main-agent prompt uses this order when context-pack prompt injection is enabled:

```text
system instructions
+ durable operational memory
+ compacted older conversation state
+ recent raw conversation turns
+ latest user message
```

Recommended behavior:

- short conversation: use full raw history plus durable memory
- long conversation: use compact context for older messages plus recent raw turns
- new conversation handoff: retrieve relevant `context_pack` plus durable memory
- workflow execution: retrieve non-sensitive `workflow`, `workspace`, or `user` scoped context packs through shared
  memory, then append operational memory
- selected workflow start: pass `contextPackId` or `context_pack_id` when creating an execution to force a readable
  active context pack into the workflow prompt

The agent should not manually decide between full and compact state. Prompt assembly should enforce the policy.

Compact pack APIs are controlled by `MEMORY_CONTEXT_PACK_ENABLED` and default to enabled. Prompt injection is controlled
by `MEMORY_CONTEXT_PACK_PROMPT_INJECTION_ENABLED` and defaults to disabled. The number of injected context packs is
controlled by `MEMORY_CONTEXT_PACK_PROMPT_LIMIT`. Automatic handoff-pack creation is controlled by
`MEMORY_CONTEXT_PACK_AUTO_CREATE_ENABLED`, which defaults to disabled. When enabled, long conversations without an
active handoff pack can create an older-than-recent pack before the model request is assembled.

Long-history prompt compaction is separately controlled by `MEMORY_CONTEXT_PACK_HISTORY_COMPACTION_ENABLED` and defaults
to disabled. It only trims raw history when an active handoff context pack exists. The policy is bounded by
`MEMORY_CONTEXT_PACK_HISTORY_MIN_MESSAGES` and keeps the latest `MEMORY_CONTEXT_PACK_HISTORY_RECENT_MESSAGES` raw
messages in the model request. `MEMORY_CONTEXT_PACK_HISTORY_MAX_RAW_TOKENS` can also trigger compaction when a
conversation is under the message-count threshold but over the estimated raw-token threshold.

## Triggering Choices

Manual triggers:

- user clicks "Compact conversation"
- user asks for a handoff
- user exports a context pack
- operator backfills compaction for old conversations

Automatic triggers:

- conversation exceeds a token threshold
- conversation has more than N messages
- day-end summarization runs
- workflow completes and has compactable output
- user starts a new workflow from an existing conversation

Suggested initial thresholds:

- compact older history when estimated prompt history exceeds 50 percent of model context budget
- keep the most recent 10 to 20 user/assistant turns raw
- regenerate compact packs when new messages exceed a delta threshold, such as 20 new messages or 4,000 new tokens

## API Shape

Endpoints:

```text
POST /conversations/{conversation_id}/compact
GET /conversations/{conversation_id}/compact-packs
GET /memories?memory_type=context_pack&source_conversation_id={conversation_id}
POST /memories/compact/backfill
```

Backfill is an admin-only memory endpoint. It supports dry-run, optional conversation filtering, limits, skip-existing
behavior, and the same mode/strategy/custom options as direct conversation compaction.

Supported source ranges:

- `full`: compact the full conversation; this is the default
- `selected`: compact the explicit `source_message_start_id` to `source_message_end_id` window
- `since_last_compact`: compact messages after the latest active context pack for the selected mode
- `older_than_recent`: compact all but the last `recent_message_limit` messages so prompt assembly can keep recent turns
  raw

Supported persistence scopes:

- `conversation`: save the pack to the source conversation; this is the default
- `user`: save the pack as user memory using the source conversation owner
- `workspace`: save the pack as workspace memory using the source conversation workspace
- `workflow`: save the pack as workflow memory; requires `workflow_id`

Workflow shared-memory config can tune context-pack reuse:

```json
{
  "shared_memory": {
    "enabled": true,
    "context_packs_enabled": true,
    "context_pack_mode": "handoff",
    "context_pack_limit": 2
  }
}
```

Workflow execution creation can pin a specific pack:

```json
{
  "workflowId": "workflow_123",
  "input": { "topic": "launch plan" },
  "contextPackId": "mem_context_pack_123",
  "trigger": { "type": "manual", "created_by": "user_123" }
}
```

Example request:

```json
{
  "mode": "handoff",
  "strategy": "deterministic",
  "token_budget": 1200,
  "source_range": "full",
  "source_message_start_id": null,
  "source_message_end_id": null,
  "recent_message_limit": 8,
  "scope": "conversation",
  "workflow_id": null,
  "persist": true,
  "confirmed": false,
  "supersede_previous": true,
  "model_profile_id": null,
  "custom_keep": null,
  "custom_drop": null
}
```

Supported strategies:

- `deterministic`: local extraction and rendering; this is the default
- `llm`: use a configured model profile for structured compaction, then fall back to deterministic output if the model
  fails
- `auto`: reserved for policy-based selection; currently behaves like LLM-first with deterministic fallback

Example response:

```json
{
  "status": "created",
  "memory_id": "mem_123",
  "mode": "handoff",
  "scope": "conversation",
  "source_range": "full",
  "source_message_count": 84,
  "estimated_compact_tokens": 1100,
  "sensitive": false,
  "warnings": [],
  "progress": {
    "completed_steps": 7,
    "failed_steps": 0,
    "events": [
      {
        "step": "select_source",
        "status": "completed",
        "message": "Source messages selected and normalized."
      },
      {
        "step": "persist",
        "status": "completed",
        "message": "Context pack persisted."
      }
    ]
  },
  "content": "..."
}
```

Example backfill request:

```json
{
  "conversation_id": null,
  "user_id": null,
  "workspace_id": null,
  "workflow_id": null,
  "mode": "handoff",
  "strategy": "deterministic",
  "token_budget": 1200,
  "source_range": "full",
  "recent_message_limit": 8,
  "scope": "conversation",
  "limit": 50,
  "dry_run": true,
  "confirmed": false,
  "skip_existing": true,
  "supersede_previous": true,
  "idempotency_key": "compact-backfill-2026-05-22",
  "model_profile_id": null,
  "custom_keep": null,
  "custom_drop": null
}
```

When `idempotency_key` is provided, retries return the existing context pack with status `existing` instead of creating
a duplicate compact memory record.

Both direct compaction and backfill return a `progress` object. This is not a streaming channel; it is a structured
event list that lets clients render step-level status, skipped work, failures, and per-conversation backfill progress
without parsing free-form summaries.

## UX Choices

Useful user-facing choices:

- mode selector: brief, handoff, memory, workflow, technical, archive, custom
- token budget: small, medium, large, custom
- output format: markdown, JSON, markdown plus JSON
- persistence: preview only or save to memory
- scope: conversation, workspace, workflow, user
- source range: full conversation, selected range, since last compact, older than recent messages
- supersede behavior: create new, replace latest, archive old

The frontend conversation workspace exposes these choices through a `Compact` action on the active chat. Users can
preview a compact output, save it as conversation memory, review saved compact packs, copy a pack, use a pack to seed a
new chat, or create a draft workflow from a saved pack. Workflow creation should preserve the pack id, mode, summary,
source conversation, and pack content in workflow metadata/task instructions so the draft remains auditable.

For most users, default to:

- mode: handoff
- output: markdown plus structured JSON
- persist: true
- scope: conversation
- supersede previous handoff pack: true

## Access And Safety

The compact tool must follow existing memory access rules.

Rules:

- never delete or rewrite raw messages during compaction
- preserve source provenance
- infer sensitivity from both source transcript and compacted output
- require `confirmed=true` before persisting sensitive compact packs through user-facing and admin backfill paths
- system-generated compact records can use trusted service paths but should still mark sensitivity
- avoid treating retrieved memory as instructions
- allow regeneration when source messages or mode schema changes

## Relationship To Existing Summary Types

`daily_summary`:

- chronological day archive
- generated on schedule
- useful for recent summary retrieval

`run_summary`:

- workflow execution result
- generated after eligible runs
- useful for workflow learning and recall

`context_pack`:

- mode-aware reusable state
- generated manually or by token thresholds
- useful for handoff, prompt compaction, workflow startup, and cross-conversation reuse

Atomic memory types:

- `fact`
- `preference`
- `decision`
- `task_commitment`

These should remain separate because they are easier to rank, supersede, and inject precisely.

## Implementation Status

Implemented:

- mode profiles for handoff, brief, memory, workflow, technical, archive, and custom modes
- durable `context_pack` records in `memory_records`
- conversation compaction and compact-pack listing APIs
- admin compact backfill
- prompt assembly support for compact older state plus recent raw turns behind feature flags
- workflow shared-memory reuse and selected-pack workflow starts
- frontend conversation controls for previewing, saving, copying, and reusing compact packs
- tests for storage, retrieval, prompt assembly, source ranges, sensitivity handling, idempotency, backfill, and workflow reuse
