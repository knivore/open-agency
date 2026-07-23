# Memory

## Overview

The backend uses a DB-first memory model.

- raw main-agent chat history lives in `conversations` and `conversation_messages`
- durable goals, plans, evidence, evaluations, and supervisor decisions live in goal records and linked execution events
- durable memory for both `main-agent` and non-main agents lives in `memory_records`
- per-execution scratch state remains ephemeral inside the runtime and is not persisted as durable memory unless a
  summary is explicitly written

There is no file-based memory layer. The source of truth is the database.
Goal summaries may be stored as durable memory when useful for future retrieval, but memory remains supporting context:
the canonical goal state stays on the goal record, execution links, evidence, evaluations, approvals, and supervisor
events.

## Memory Layers

### Main-agent

The `main-agent` uses two persistent layers:

- conversation history: raw user/assistant/tool interaction in `conversation_messages`
- durable memory: retrieved `memory_records` injected into prompt context

This separation matters:

- raw chat history preserves the exact interaction log
- durable memory stores reusable facts, preferences, summaries, decisions, and commitments
- daily summarization adds compact historical context without rewriting source chat history

### Other agents

Non-main agents do not require conversation tables.

They can:

- read shared durable memory from `memory_records` during native runtime task prompt assembly
- optionally write durable `run_summary` records at execution completion
- keep intermediate runtime state ephemeral during the run

Native runtime shared memory is opt-in. Enable it through workflow metadata:

```json
{
  "shared_memory": {
    "enabled": true,
    "limit_per_layer": {
      "decisions": 4,
      "commitments": 4,
      "facts_and_preferences": 6,
      "recent_summaries": 3,
      "semantic_fallback": 3
    }
  }
}
```

It is also enabled for an agent when `memory.enabled=true` and `memory.scope` is not `execution`. Use `workflow` scope
for memory shared by agents inside one workflow, `workspace` scope for project-level memory shared across workflows, and
`user` scope for user-specific preferences. `agent_id` should be treated as producer or attribution metadata, not as the
primary sharing boundary.

Operators can read and update workflow shared-memory settings without replacing the full workflow definition:

- `GET /workflows/{workflow_id}/shared-memory`
- `PATCH /workflows/{workflow_id}/shared-memory`

The patch accepts `enabled`, optional `limit_per_layer`, and `apply_to_agents=true` when the embedded workflow agents
should also be marked with `memory.enabled=true`.

## Persistent Storage

Durable memory is stored in `memory_records`.

Important fields:

- scope fields: `scope`, `created_by_user_id`, `workspace_id`, `conversation_id`, `workflow_id`, `agent_id`
- content fields: `content`, `summary`, `tags_json`, `metadata_json`
- retrieval/ranking fields: `memory_type`, `status`, `importance`, `last_used_at`, `updated_at`
- summary/archive fields: `summary_date`, `archived_window_start`, `archived_window_end`,
  `source_conversation_id`, `source_execution_id`, `supersedes_memory_id`
- embedding fields: `embedding_json`, `embedding_model_profile_id`, `embedding_model`, `embedding_dimensions`,
  `embedded_at`

## Scopes

Supported durable-memory scopes:

- `user`: user-specific preferences and durable facts
- `workspace`: workspace/team/project memory
- `conversation`: memory tied to one conversation
- `workflow`: workflow-specific durable memory
- `global`: deployment-level system memory

## Memory Types

The backend stores the raw field as `memory_type`, but product surfaces should call these records **memory types**.
Current durable-memory types, product-facing labels, and representations:

| Raw `memory_type` | Product label    | Represents                                                                          |
|-------------------|------------------|-------------------------------------------------------------------------------------|
| `preference`      | Preferences      | Stable preferences, such as "user prefers concise updates."                         |
| `fact`            | Facts            | Durable knowledge, such as "workspace uses Postgres for memory records."            |
| `decision`        | Decisions        | Chosen directions, such as "use memory links in workflow metadata for now."         |
| `task_commitment` | Task Commitments | Obligations, promised actions, or task cues that should influence future execution. |
| `daily_summary`   | Conversation     | Conversation summaries and conversation-scoped recall for prior discussion context. |
| `archive`         | Files            | Uploaded documents and extracted file chunks available for retrieval.               |
| `context_pack`    | Compact Packs    | Compacted conversation or workflow handoff packs for reuse in other contexts.       |
| `run_summary`     | Run Summaries    | Execution summaries written by workflow runs for future operational recall.         |

Legacy records may still exist with no explicit `memory_type`. They remain readable and retrievable.

`context_pack` records are derived compact state from a source conversation or workflow. Conversation compaction can
persist packs as conversation, user, workspace, or workflow scoped memory depending on the intended reuse boundary.
Workflow shared-memory prompt assembly retrieves non-sensitive scoped context packs explicitly, separate from generic
operational memory, so compact packs do not pollute normal fact/decision ranking.

Native runtime context compaction can also persist workflow-scoped `context_pack` records when a workflow execution's
estimated context health reaches `critical` or `overflow`, but this persistence is opt-in. Set
`workflow.metadata.runtime_governance.context_compaction.persist_context_pack=true` for workflows that should retain
runtime compaction summaries as memory. Deployments can override the fallback default with
`AGENT_CONTEXT_COMPACTION_PERSIST_CONTEXT_PACK_DEFAULT=false|true`; the default is `false`. These packs use
`source="runtime_context_compaction"`, `memory_type="context_pack"`, and `source_execution_id` to bind the compacted
summary back to the execution audit trail. They are derived prompt-management artifacts, not a replacement for raw
execution events or tool outputs.

## Memory Ops And Contextual Memory Actions

Memory Ops is the operator console for durable memory. It is useful for inspection, cleanup, backfill, and cross-context
administration, but it is not the only place memory actions should happen.

The product split is:

- Memory Ops manages and audits memory records across the system.
- Assistant, conversation, workflow, agent, and task surfaces create or attach memory in the place where the operator is
  already working.
- Backend APIs and system tools are the source of truth. The frontend is an operator UI over those contracts, not the
  only way to manage memory.

Current Memory Ops tabs map to backend capabilities as follows:

- `Browse`: searches raw durable memory records through `GET /memories`, opens detail/lineage, and supports edit,
  delete, archive/status changes, copy, source links, and exclusions.
- `Create`: writes focused manual memory records through `POST /memories` or corrects existing records through
  `PATCH /memories/{memory_id}`. The guided forms set scope, memory type, summary, content, tags, importance,
  sensitivity, confirmation, and optional workflow/agent/conversation binding.
- `Ingest Files`: uploads source documents through `POST /documents/ingest`; the backend can save them for retrieval,
  attach them as immediate model context, or do both. Retrieval uploads create `archive` memories grouped by document id.
  Upload surfaces can first call `POST /documents/intelligence`, which uses the active main-agent model profile to
  recommend document kind, scope, agent/workflow/conversation binding, tags, chunking, and persona governance labels.
  This can be used from Memory Ops for administrative ingestion, or from contextual surfaces when a file belongs to a
  conversation, workflow, task, or agent.
- `Analyze Sources`: classifies selected memories through `POST /memories/source-intelligence/analyze`. The backend can
  use a model profile to label document kind, content role, extraction targets, memory layers, vector tags, and graph
  hints. Results are persisted into memory metadata with review status. Approved graph hints emit a projection event
  that maps reviewed entities and relationships into Neo4j.
- `Compact Conversations`: previews or persists `context_pack` memories through conversation compaction APIs and runs
  admin backfills through `POST /memories/compact/backfill`.
- `Summaries`: reviews and runs daily/run summary flows without mixing summaries into normal manual-memory workflows.
- `Maintenance`: runs vector embedding backfill and other memory maintenance actions.

Contextual surfaces should prefer narrower actions:

- Assistant or conversation pages can expose "compact this conversation", "save pack", "use in chat", "create workflow
  from compact pack", and file upload into conversation/workflow memory.
- Agency Graph memory nodes, agent inspectors, and task inspectors should use the memory catalog and workflow memory
  link APIs to attach or remove memory for that exact graph target.
- Agent or task drawers can expose "remember for this agent/task", "attach document memory", and "exclude this memory
  for this target" without sending the operator to the global Memory Ops page.
- Memory Ops remains the fallback for advanced search, broad cleanup, superseded pack review, backfills, and debugging.

Conversation compaction can be invoked manually from the Assistant/conversation UI or administratively from Memory Ops.
Automation hooks should use the same backend compaction service. Useful trigger points are:

- when a conversation grows beyond the configured raw-history threshold
- before creating a workflow from a long chat
- after a workflow-planning conversation reaches a stable handoff point
- from scheduled/admin backfill jobs

Document ingestion is scope-aware. Use:

- `user` for personal reusable knowledge
- `workspace` for team or project knowledge
- `conversation` for files relevant to one chat thread
- `workflow` for workflow-level knowledge
- optional `agent_id` when the document should be biased toward a specific agent

The ingestion API deliberately does not accept `global` scope for uploaded files. Source material should have a concrete
owner or operational context.

`POST /documents/ingest` accepts `upload_mode=vector|context|both`. Missing `upload_mode` defaults to `vector`.

- `vector`: extract and chunk into durable `archive` memory for retrieval.
- `context`: create an uploaded-document reference and inject the extracted text into the next conversation prompt only.
- `both`: attach the document to the immediate prompt and also save archive chunks for retrieval.

Direct-context attachments are stored as uploaded-document records and referenced from conversation message metadata via
`context_attachment_ids`. Extracted text is not copied into message rows. The prompt formatter treats direct-context text
as untrusted source material, not instructions.

Conversation context diagnostics include a `direct_document_context` block with attachment count, included/skipped
documents, estimated attachment tokens, and the per-document inclusion status. This keeps direct-context uploads visible
in context-health checks instead of hiding them inside the instruction estimate.

Document drawers and file lists should read `GET /documents` first so context-only uploads remain visible even when no
archive memory chunks exist. `DELETE /documents/{document_id}` is the preferred lifecycle endpoint: context-only
documents are tombstoned and have extracted text cleared; `vector` and `both` documents also delete related archive
memory chunks and emit the existing document collection deletion projection. Product labels should distinguish
`Context only`, `Retrieval`, and `Context + retrieval`.

When `auto_intelligence=true` is sent to `POST /documents/ingest`, Agency applies main-agent upload recommendations
where the caller allows them. Contextual surfaces should lock known scope/bindings, while Memory Ops can allow broader
scope and agent suggestions. Each chunk stores `metadata.upload_intelligence` with the recommendation source, model
profile id, recommended values, and the settings actually applied. Uploaded-document records also retain the
recommendation metadata for context-only uploads that do not create chunks.

Uploaded-document records retain `metadata.upload_observability`, which records the upload mode, estimated size,
direct-context attachment id, chunk count, memory ids, and whether a document collection graph projection event was
created.

This is an Agency-wide document ingestion contract, not a Persona Factory-only path. Conversation uploads, Memory Ops,
agent documents, workflow documents, graph task or memory-node uploads, and future tool knowledge upload surfaces should
all call the same backend route or shared frontend control so classification, tags, routing, chunking, and governance
defaults stay consistent.

Deferred ingestion work should stay in this document instead of a separate checklist. Current open areas:

- Define exact upload-mode availability and defaults for every surface: conversation, Memory Ops, agent documents,
  workflow documents, task uploads, graph memory nodes, and future tool knowledge uploads.
- Formalize hard limits for raw upload size, extracted text length, direct-context token budget, and attachment count in
  user-facing API errors.
- Keep extracted text storage explicit: small text may live in the uploaded-document row, while larger text should move
  to managed storage with only URI and metadata in Postgres.
- Decide duplicate upload behavior for scoped uploaded-document rows versus reused storage blobs.
- Keep `DELETE /documents/{document_id}` as the lifecycle endpoint and preserve the current rule: context-only documents
  clear extracted text, while `vector` and `both` uploads also remove related archive chunks.
- Improve prompt rendering for archive document chunks so retrieved document memories include chunk `content` with stable
  source labels, while manual facts/preferences/decisions remain summary-first.
- Add coverage for uploaded-document repository scope filters, prompt rendering of document chunks, direct-context
  compaction boundaries, upload-mode errors, accessibility, and conversation E2E flows for `context`, `vector`, and
  `both`.
- Keep observability content-free: record upload mode, extracted size, estimated direct-context tokens, truncation status,
  created memory ids, and graph projection status without logging document bodies.
- Decide whether historical archive-memory document collections need uploaded-document backfill. Skip backfill unless a
  product surface requires old files to appear in `GET /documents`.

Persona Factory consumes the same document intelligence instead of reclassifying from raw text alone. Distillation first
honors stored `metadata.source_intelligence.classification`, then falls back to `metadata.upload_intelligence`, then to
deterministic source heuristics. Extracted persona items retain the selected routing payload, distiller name/version,
source document id, filename, chunk index, content hash, storage URI, upload mode, and review status so later package
synthesis remains auditable.

Memory source intelligence is review-first. It stores:

- `metadata.source_intelligence`: classifier output, model profile, source reference, and review status.
- `metadata.graph_hints`: proposed graph entities and relationships with independent review status.
- `metadata.vector_tags`: model-suggested retrieval tags for filtered vector search.

Use `PATCH /memories/{memory_id}/source-intelligence` to approve, reject, or edit this metadata. Only approved graph
hints on active memories are projected into Neo4j. Re-approving unchanged graph hints reuses a stable source event id,
so the projection outbox remains idempotent while changed hints can emit a new reviewed projection event.

Published persona invocation can also consume this graph layer. When graph context auto-retrieval is enabled, Agency
loads the persona graph lineage and injects a concise "Persona Graph Context" prompt section alongside the reviewed
persona memory. The graph context is supporting evidence and does not replace the source-backed persona package or
approved persona memories.

## Main-Agent Retrieval

The current main-agent retrieval model is layered and flag-gated by `MEMORY_RETRIEVAL_V2_ENABLED`.

When enabled, prompt assembly prefers:

1. recent raw conversation turns
2. active `decision` and `task_commitment` memories
3. `fact` and `preference` memories
4. recent `daily_summary` memories
5. additional relevant fallback memory

Sensitive durable memories are excluded from prompt injection by default.

If Retrieval V2 is disabled, the main-agent falls back to the older scoped durable-memory retrieval path.

Agency Graph context is separate from durable/vector memory. Durable memory answers semantic recall questions such as
user preferences, project facts, summaries, and active commitments. `agency.graph.context`, when enabled with
`AGENCY_GRAPH_CONTEXT_TOOLS_ENABLED`, answers relationship and lineage questions: which workflow, run, task, agent,
memory, document, error, decision, or next action is connected to the current work. Agents should use graph context for
resume, debug, handoff, audit, and root-cause tasks, then fall back to durable memory when graph projection is disabled,
unavailable, or has no matching records.

Context-pack creation and listing are globally gated by `MEMORY_CONTEXT_PACK_ENABLED`, which defaults to enabled.
Context-pack prompt injection is separately gated by `MEMORY_CONTEXT_PACK_PROMPT_INJECTION_ENABLED` and defaults to
disabled. When enabled, prompt assembly can include recent active `context_pack` records before normal operational
memory. `MEMORY_CONTEXT_PACK_PROMPT_LIMIT` controls how many packs are injected.

`MEMORY_CONTEXT_PACK_AUTO_CREATE_ENABLED` controls best-effort automatic handoff-pack creation for long conversations and
defaults to disabled. When enabled, prompt assembly can create an older-than-recent handoff pack before model request
assembly if no active handoff pack exists.
`MEMORY_CONTEXT_PACK_HISTORY_COMPACTION_ENABLED` controls whether long prompt history may be shortened when an active
handoff context pack exists. `MEMORY_CONTEXT_PACK_HISTORY_MIN_MESSAGES` sets the minimum raw message count before
compaction is considered, and `MEMORY_CONTEXT_PACK_HISTORY_RECENT_MESSAGES` controls how many recent raw messages remain
verbatim in the model request. `MEMORY_CONTEXT_PACK_HISTORY_MAX_RAW_TOKENS` adds an estimated-token threshold for
shorter but token-heavy conversations.

## Daily Summaries

The system supports one durable `daily_summary` per conversation per day.

Behavior:

- summaries are written into `memory_records`
- raw conversation history is not mutated or deleted
- duplicate summaries for the same conversation and day are skipped
- summaries include `summary_date`, source conversation provenance, and archived window bounds

Automatic daily-summary generation is controlled by:

- `MEMORY_DAILY_SUMMARY_ENABLED`
- `MEMORY_DAILY_SUMMARY_TIMEZONE`
- `MEMORY_DAILY_SUMMARY_TARGET_HOUR`
- `MEMORY_DAILY_SUMMARY_TARGET_MINUTE`
- `MEMORY_DAILY_SUMMARY_INTERVAL_SECONDS`

The app runs a background loop that attempts the previous local day after the configured local target time.

## Non-Main-Agent Run Summaries

Non-main agents can write durable `run_summary` records at execution completion.

Behavior:

- globally gated by `AGENT_PERSISTENT_RUN_SUMMARY_ENABLED`
- additionally opt-in per workflow through workflow metadata
- writes are suppressed for near-duplicate recent executions
- failure summaries are disabled by default unless the workflow config explicitly allows them

Keep `AGENT_PERSISTENT_RUN_SUMMARY_ENABLED=false` until the deployment is ready for durable workflow learning. Enabling
the global flag only opens the outer gate; each workflow must still opt in with `main_agent_monitoring` summary settings
and `safe_to_summarize=true`.

## Ranking And Embeddings

Memory retrieval uses hybrid ranking.

- if `MEMORY_EMBEDDING_MODEL_PROFILE_ID` is configured, memory writes store embeddings and retrieval uses vector-aware
  ranking plus lexical scoring
- on Postgres, long-term memory embeddings are stored in pgvector column `memory_records.embedding_vector`; the existing
  `embedding_json` field remains as a fallback/compatibility copy
- otherwise retrieval falls back to deterministic lexical and scope-aware ranking

Embedding-related settings:

- `MEMORY_VECTOR_RETRIEVAL_ENABLED`
- `MEMORY_EMBEDDING_MODEL_PROFILE_ID`
- `MEMORY_EMBEDDING_WRITE_ERRORS_STRICT`

Use `.venv/bin/python scripts/setup.py embedding-agent` or `make setup-embedding-agent` to create the default Ollama
embedding profile and `Embedding` agent.

Use `POST /memories/embeddings/backfill` to embed older durable-memory rows after configuring an embedding profile.

## Document Ingestion

Uploaded documents can be ingested directly into durable memory or attached to a single model turn through
`POST /documents/ingest`.

The ingestion pipeline:

1. stores the original uploaded file through Agency storage
2. extracts text from the document
3. stores an `uploaded_documents` record with content hash, scope, mode, estimated tokens, and extracted text reference
4. for `vector` or `both`, normalizes and chunks the text
5. for `vector` or `both`, writes each chunk as an active `archive` memory record with `source=document_upload`
6. embeds each chunk through the configured `MEMORY_EMBEDDING_MODEL_PROFILE_ID`
7. emits a document collection graph projection event for `vector` or `both` only
8. retrieves matching chunks later through the same pgvector-backed memory search path used by chat and agents

Supported upload extensions are `.txt`, `.md`, `.markdown`, `.csv`, `.json`, `.log`, `.html`, `.htm`, `.pdf`, and
`.docx`.

Each chunk stores document provenance in `metadata_json`, including `document_id`, `filename`, `storage_uri`,
`content_sha256`, `upload_mode`, `chunk_index`, `chunk_count`, character offsets, and source-intelligence hints when
available. Chunks use `memory_type=archive`, so they are available for semantic recall without pretending the full
source document is a manually curated fact or preference. Context-only uploads do not create archive chunks and
therefore do not enter vector search or graph projection unless re-uploaded or saved with `upload_mode=both`.

Document ingestion accepts `user`, `workspace`, `conversation`, and `workflow` scopes. It deliberately does not accept
`global` scope because uploaded source material should have an explicit owner or operational context. Workspace,
conversation, and workflow ingestion must include the matching `workspace_id`, `conversation_id`, or `workflow_id`.
Any document scope can also include an optional `agent_id` binding for agent-specific recall.

## Memory Catalog, Exclusions, And Workflow Links

The memory catalog is the backend contract used by Agency Graph memory nodes, workflow editors, and picker UIs:

- `GET /memories/catalog`
- groups records into manual memories, compact packs, conversation summaries, document groups, and run summaries
- returns individual memories as `refType=memory`
- returns document groups as `refType=memory_collection`
- includes linkability fields such as `canLink`, `blockedReason`, `excluded`, `exclusionReason`, and `memoryIds`

Memory exclusions are non-destructive skips. They do not delete or archive the memory; they only prevent use for a
matching target context.

- `GET /memories/exclusions`
- `POST /memories/{memory_id}/exclusions`
- `DELETE /memories/{memory_id}/exclusions/{exclusion_id}`

Supported exclusion targets are `global`, `workflow`, `agent`, `task`, `conversation`, and `run`.

Workflow memory links attach a memory resource to a workflow graph target:

- `GET /workflows/{workflow_id}/memory-links`
- `POST /workflows/{workflow_id}/memory-links`
- `DELETE /workflows/{workflow_id}/memory-links/{link_id}`

Links are stored in workflow metadata under `memory_links` for the current migration-free implementation. Link targets
can be:

- `workflow`
- `agent`
- `task`

Link refs can be:

- `memory`: one memory id
- `memory_collection`: a document group, expanded into underlying chunk memory ids

`access_mode=read` means the linked target can use the memory as context. `access_mode=read_write` additionally allows
agent tool execution to mutate the linked memory when the tool call supplies the matching workflow/target context.
Runtime memory update/delete calls made through workflow link context are blocked unless a matching `read_write` link
exists.

## Agent-Operable Memory Tools

Agents can manage memory through system tool contracts when the backend context is available. The current memory tool
surface includes:

- `agency.memory.list`
- `agency.memory.catalog`
- `agency.memory.remember`
- `agency.memory.update`
- `agency.memory.delete`
- `agency.memory.exclusions.list`
- `agency.memory.exclusions.add`
- `agency.memory.exclusions.delete`
- `agency.workflow.memory-links.list`
- `agency.workflow.memory-links.add`
- `agency.workflow.memory-links.delete`

These tools call the same memory services and workflow metadata contracts used by the HTTP APIs. Mutating tools remain
policy-labelled as mutations, and sensitive memory writes still require explicit confirmation.

## API Surface

Core durable-memory routes:

- `POST /documents/ingest`
- `GET /documents`
- `GET /documents/{document_id}`
- `DELETE /documents/{document_id}`
- `GET /memories`
- `GET /memories/catalog`
- `GET /memories/exclusions`
- `GET /memories/source-intelligence/catalog`
- `POST /memories/source-intelligence/analyze`
- `POST /memories`
- `GET /memories/{memory_id}`
- `PATCH /memories/{memory_id}`
- `PATCH /memories/{memory_id}/source-intelligence`
- `DELETE /memories/{memory_id}`
- `POST /memories/{memory_id}/exclusions`
- `DELETE /memories/{memory_id}/exclusions/{exclusion_id}`
- `POST /memories/embeddings/backfill`

Summary/admin routes:

- `POST /memories/daily-summaries/run`
- `POST /memories/daily-summaries/backfill`
- `POST /memories/compact/backfill`

Conversation compact routes:

- `POST /conversations/{conversation_id}/compact`
- `GET /conversations/{conversation_id}/compact-packs`

Workflow memory-link routes:

- `GET /workflows/{workflow_id}/memory-links`
- `POST /workflows/{workflow_id}/memory-links`
- `DELETE /workflows/{workflow_id}/memory-links/{link_id}`

Useful list filters:

- `scope`
- `user_id`
- `workspace_id`
- `conversation_id`
- `workflow_id`
- `agent_id`
- `source`
- `memory_type`
- `status`
- `source_conversation_id`
- `source_execution_id`
- `summary_date_from`
- `summary_date_to`
- `q`

## Access Rules

Durable-memory access follows the existing ownership model:

- user memories are restricted to the matching `created_by_user_id`
- workspace memories rely on ownership/trust metadata such as `owner_ids`, `created_by`, and `trusted_user_ids`
- conversation memories require ownership of the linked conversation
- workflow memories require ownership of the linked workflow
- admins can access all memories

Sensitive writes require `confirmed=true` for normal CRUD paths. Context-pack compaction applies the same confirmation
rule and marks a pack sensitive if either the source transcript or compacted output matches sensitive markers.

System-generated summaries do not require human confirmation because they are generated internally through trusted
service paths. Automatic context-pack creation still uses the sensitive-write guard and skips creation if confirmation
would be required.

## Operational Notes

- `memory_records` is the shared persistent memory table for all agents
- Retrieval V2, daily summaries, and run summaries can all be rolled back behaviorally with feature flags
