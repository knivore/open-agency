# Persona Factory Backend Guide

Persona is Agency's canonical backend, API, and product term for a reusable simulated identity/expertise package. Other ecosystems may call a similar artifact a skill. In Agency, skill-style files are only import/export interoperability around the canonical Persona model.

This guide is the backend source of truth for maintaining Persona, Persona Factory, distillation, publishing, runtime invocation, and Persona Graph RAG. Operator entry points belong in `docs/runbook.md`; curl-heavy Persona examples remain in `docs/persona-factory-cli.md` until durable steps are folded into the platform runbook.

## Mental Model

Use these boundaries when changing code or writing docs:

- Memory is what Agency knows. It stores source chunks, extracted durable memories, source intelligence, graph hints, and published persona memory records.
- Agent is what acts. It owns executable agent instructions, model profile binding, tool binding, and runtime execution.
- Tool is what does side effects. It has schemas, permissions, and executor/integration behavior.
- Persona is who or what the agent acts as. It packages source-backed style, judgement, knowledge, workflows, guardrails, examples, governance labels, and memory layers.
- Persona Factory is how raw source material becomes a governed Persona package.

The backend must remain usable without `open-agency-fe`. Every Persona workflow should be possible through API or CLI.

## Lifecycle

The intended flow is:

```text
Raw uploads
-> parse
-> chunk
-> classify
-> extract
-> summarize/normalize
-> validate
-> store reviewable Persona distillation items
-> synthesize Persona package
-> review
-> approve
-> publish
-> invoke with @persona-slug
```

The current implementation maps this flow to:

1. Upload documents or create memories through the shared Memory/Document APIs.
2. Optionally classify uploaded chunks with source intelligence.
3. Distill selected source memories through `POST /persona-factory/distill`.
4. Review extracted `persona_distillation_items`.
5. Normalize, correct source classification, or re-distill one source when needed.
6. Synthesize a package from active items.
7. Approve a Persona version.
8. Publish the version, which materializes an agent and persona-scoped memory.
9. Mention `@slug` or `@slug:version` in conversation runtime.

## Source Of Truth

Postgres and the repositories are the source of truth:

- `personas`: catalog record, lifecycle status, current version, published agent reference.
- `persona_versions`: immutable-ish reviewed package snapshots, version labels, approval/publish status.
- `persona_sources`: source references linked to a Persona.
- `persona_distillation_runs`: distillation job state, selected sources, draft package, warnings, errors.
- `persona_distillation_items`: small reviewable extracted records.
- `memory_records`: raw source chunks, source intelligence, graph hints, and published persona memories.
- `agents`: materialized runtime agent created from the published Persona package.

Neo4j is a rebuildable projection. Do not treat Neo4j as the Persona source of truth.

## Repository Map

Core Persona code:

- `app/domain/personas.py`: Pydantic domain contracts, statuses, item types, memory layers, review states.
- `app/db/models/personas.py`: SQLAlchemy table mappings.
- `app/db/repositories/personas.py`: in-memory and SQL repository implementations.
- `app/services/personas.py`: Persona catalog, import/export, and agent instruction materialization.
- `app/services/persona_factory.py`: distillation, review, package synthesis, approval, publishing, feedback, rollback.
- `app/services/persona_distillation_pipeline.py`: deterministic source classification and specialized distillers.
- `app/services/persona_graph_context.py`: runtime and inspection graph context policy/rendering.
- `app/api/routes/persona.py`: `/persona` and `/persona-factory` APIs.
- `app/services/conversations/core.py`: `@persona` runtime invocation.

Shared ingestion and graph code:

- `app/services/document_ingestion.py`: Agency-wide upload intelligence and document chunking.
- `app/services/source_intelligence.py`: shared LLM/deterministic source classification, vector tags, graph hints.
- `app/services/memory.py`: memory CRUD and source-intelligence persistence.
- `app/graph/delta.py`, `app/graph/neo4j_projection.py`, `app/graph/parity.py`: Persona projection events and parity.
- `app/graph/neo4j_read.py`: graph read presets including `persona_lineage` and `persona_capability_map`.

Important tests:

- `tests/test_persona_domain_repositories.py`
- `tests/test_persona_distillation_pipeline.py`
- `tests/test_persona_factory_api.py`
- `tests/test_persona_package_synthesis.py`
- `tests/test_workflow_persona_versions.py`
- `tests/conversations/test_main_agent.py`
- `tests/test_graph_parity.py`
- `tests/test_neo4j_graph_projection.py`
- `tests/test_api_main.py`

## Distillation

Persona Factory does not make one large profile summary. It creates many small structured items that can be reviewed, normalized, rejected, superseded, and traced to source.

Source classification priority:

1. `memory.metadata.source_intelligence.classification`
2. `memory.metadata.upload_intelligence`
3. deterministic heuristics in `PersonaDistillationPipeline.classify`

Specialized deterministic distillers extract:

- domain knowledge
- procedures and workflows
- decision patterns
- writing style
- tool usage
- examples
- guardrails

Each `PersonaDistillationItem` stores item type, memory layer, title, content, confidence, review state, source memory id, structured source refs, routing payload, distiller name/version, and metadata. Deterministic, LLM, and hybrid paths all write this same item contract instead of bypassing review.

Persona Factory supports three distillation modes:

- `deterministic`: the bounded local pipeline classifies sources and runs deterministic specialized distillers. This remains selectable as a fallback, baseline, and low-cost option.
- `llm`: LLM specialized distillers produce reviewable item candidates directly from source chunks.
- `hybrid`: deterministic and LLM candidates are both produced, then merged before review.

LLM distillation can use the active main-agent model profile, an explicit model profile, or an inline provider/model pair. `main_agent` is the default model source when LLM-backed mode is selected. Existing clients that send `distillation_mode="llm"` plus only `model_profile_id` are mapped to `llm_model_source="model_profile"` for backward compatibility.

The product default is `llm`. Treat `llm` as the LLM-first extraction path, `deterministic` as the fallback/regression
baseline, and `hybrid` as the comparison/merge path. Keep tracking live quality, cost, and latency before removing any
operator-facing mode options.

LLM distillers are the main driver in `llm` mode, but they still write the same reviewable item contract. They must emit schema-valid structured candidates with item type, memory layer, title, content, confidence, source evidence, source span, inference type, review reasons, and optional graph hints. The engine validates evidence grounding against the selected source memory. Unsupported evidence, missing spans, weak evidence, high unsupported-claim risk, and conflict signals mark items for review.

Hybrid mode keeps deterministic extraction as a fallback and comparator. Exact and semantic duplicate candidates are merged while preserving both deterministic and LLM provenance. Conflicting or one-sided claims stay reviewable with metadata such as extraction source, merged distillers, review flags, conflict group id, and reviewer actions. Review APIs can filter by extraction source, distiller, review flag, and conflict group, and can apply actions such as prefer LLM, prefer deterministic, manual merge, or mark evidence insufficient.

LLM-backed paths require schema-valid structured output. Do not let an LLM directly publish package content without item-level provenance and review.

## Memory Layers

Persona memory layers are defined in `PersonaMemoryLayer`:

- `semantic`: facts, standards, policies, domain knowledge.
- `procedural`: workflows, SOPs, decision rules, how work gets done.
- `episodic`: examples, prior incidents, audit examples, lesson records.
- `persona`: writing style, communication preferences, response style.
- `tool`: tools and systems commonly used.
- `social`: people, stakeholders, reviewers, escalation relationships.

Publishing writes approved package entries back into `memory_records` with `source="persona_factory"` and metadata such as `persona_id`, `persona_slug`, `persona_version_id`, `memory_layer`, `distillation_item_id`, `review_status`, `confidence`, `source_refs`, and `distillation_run_id`.

Runtime prefers approved persona memories over raw source chunks. Raw source memory fallback is only used when approved persona memory is unavailable.

## Governance

Governance labels are part of the package and are discoverable through `GET /persona-factory/governance-labels`.

Current label families:

- `persona_type`: `professional`, `personal`, `public_figure`, `fictional`, `self`
- `capability_mode`: `persona_only`, `expertise_only`, `persona_plus_expertise`
- `consent_status`: `unspecified`, `self`, `explicit_consent`, `organization_authorized`, `public_material`, `fictional`, `unverified_private_person`
- `source_basis`: `memory_records`, `uploaded_private_material`, `public_sources`, `user_description`, `chat_export`, `mixed`
- `sensitivity_level`: `standard`, `sensitive`, `intimate`, `regulated`
- `visibility`: `private`, `workspace`, `organization`, `marketplace`
- `representation_policy`: currently `simulated_persona`

Important invariants:

- Personal, intimate, regulated, self, public-figure, and unverified-private-person personas require explicit item approval before package approval.
- Unverified private-person personas must remain private.
- Intimate personas cannot use organization or marketplace visibility.
- Marketplace labels exist for future governance, but Agency currently excludes persona marketplace publishing.
- Generated instructions must represent the Persona as a simulated persona based on source material, not as the actual person.
- If evidence is missing, low confidence, or conflicting, runtime should state the gap instead of inventing.

## Package Schema

Persona packages use `schema_version: 1`. Required structural sections are:

- `identity`
- `persona`
- `governance`
- `knowledge`
- `decision_patterns`
- `workflows`
- `tools`
- `guardrails`
- `examples`
- `memory_layers`
- `runtime`
- `provenance`

`PersonaFactoryService._validate_package` enforces the basic shape. `PersonaFactoryService._validate_package_review_ready` blocks approval when active item-synthesized entries still need review or when higher-risk governance labels require explicit item approval.

`PersonaService.export_persona(..., format="skill_markdown")` can produce skill-style files such as `skill.md`, `persona.md`, `workflow.md`, and `tools.yaml`. This is an adapter only. Do not reintroduce `/skills`, `/skill-factory`, or `/personas` compatibility endpoints.

## Publishing And Runtime

Publishing performs three durable actions:

1. Marks the selected Persona version as published.
2. Materializes or updates a canonical `AgentDefinition` with `metadata.generated_from_persona_factory=true`.
3. Writes approved package memory layers as persona-scoped `memory_records`.

`@persona` invocation is handled in `ConversationService._maybe_handle_persona_invocation`.

Runtime behavior:

- `@slug` resolves the current published Persona version.
- `@slug:version` resolves a specific published version label or version id.
- Unknown slugs return `persona_error: "not_found"`.
- Draft, in-review, approved-but-unpublished, and archived personas return `persona_error: "not_published"`.
- Missing published agent/version records return `persona_error: "runtime_incomplete"`.
- Assistant message metadata includes `persona_provenance` with package strategy, source ids, source memory ids, distillation item ids, source refs, and runtime-context trace.

Prompt composition priority:

1. Published Persona package and materialized agent instructions.
2. Approved persona-scoped memory.
3. Persona graph context when enabled and available.
4. Normal conversation context.

If graph context conflicts with approved memory or the package, prefer the approved memory/package.

## Graph RAG

Persona lifecycle and runtime events are appended as graph projection events when `GRAPH_PROJECTION_ENABLED` is enabled. Projection events should not block primary Persona writes.

Neo4j projection creates and links:

- `Persona`
- `PersonaVersion`
- `DistillationRun`
- `DistillationItem`
- `Memory`
- `Tool`
- `Workflow`
- `Artifact`
- materialized `Agent`
- conversation invocation context

Key relationships include:

- `PERSONA_HAS_DISTILLATION_RUN`
- `RUN_EXTRACTED_ITEM`
- `ITEM_DERIVED_FROM_MEMORY`
- `RUN_USED_SOURCE_MEMORY`
- `PERSONA_HAS_VERSION`
- `RUN_PRODUCED_VERSION`
- `PERSONA_PUBLISHED_MEMORY`
- `PERSONA_USES_TOOL`
- `PERSONA_FOLLOWS_WORKFLOW`
- `PERSONA_PRODUCES_ARTIFACT`
- `PERSONA_MATERIALIZED_AS_AGENT`
- `PERSONA_INVOKED_IN_CONVERSATION`

Read presets:

- `persona_lineage`: runtime default and source-lineage inspection preset.
- `persona_capability_map`: inspection preset for what the Persona uses, follows, or produces.

Runtime graph policy lives in `PERSONA_GRAPH_CONTEXT_RUNTIME_POLICY`:

- preset: `persona_lineage`
- default limit: 24
- fallback: skip graph context without failing invocation
- conflict rule: approved package and persona memory outrank graph context

Freshly published personas may show empty graph context until pending projection events are processed.

For LLM and hybrid distillation, Neo4j matters as the discoverability and runtime-context layer, not as the authority for persona truth. LLM candidates can propose graph entity and relationship hints, but those hints remain pending until the associated distillation item is approved. Approved packages, approved persona-scoped memory, and item provenance outrank projected graph context. If Neo4j is unavailable, Persona Factory distillation, review, publishing, and runtime invocation should continue; the graph context section simply degrades or waits for projection catch-up.

## Agency-Wide Ingestion Contract

Document ingestion is not Persona-only. The same upload intelligence should be used by Memory Ops, conversations, workflows, tasks, agents, tools, graph surfaces, and Persona Factory.

Relevant endpoints:

- `POST /documents/intelligence`: classify/recommend before ingest.
- `POST /documents/ingest`: parse, chunk, store, and optionally apply intelligence.
- `POST /memories/source-intelligence/analyze`: classify existing memories.
- `PATCH /memories/{memory_id}/source-intelligence`: review/edit/approve classification and graph hints.

Persona Factory consumes existing memory metadata from those shared paths. Keep future upload UX and API behavior centralized rather than adding Persona-only upload semantics.

## API Surface

Catalog:

- `GET /persona`
- `POST /persona`
- `GET /persona/{persona_id}`
- `PATCH /persona/{persona_id}`
- `DELETE /persona/{persona_id}`
- `GET /persona/{persona_id}/versions`
- `POST /persona/{persona_id}/versions/{version_id}/rollback`
- `GET /persona/{persona_id}/sources`
- `POST /persona/{persona_id}/sources`
- `GET /persona/{persona_id}/export`
- `POST /persona/import`
- `GET /persona/{persona_id}/graph-context`
- `GET /persona/{persona_id}/workflow-usages`

Factory:

- `GET /persona-factory/governance-labels`
- `GET /persona-factory/item-types`
- `POST /persona-factory/distill`
- `POST /persona-factory/feedback`
- `GET /persona-factory/runs`
- `GET /persona-factory/runs/{run_id}`
- `GET /persona-factory/runs/{run_id}/review-summary`
- `GET /persona-factory/runs/{run_id}/items`
- `GET /persona-factory/runs/{run_id}/source-map`
- `GET /persona-factory/runs/{run_id}/sources/{source_key}`
- `PATCH /persona-factory/runs/{run_id}/sources/{source_key}/classification`
- `POST /persona-factory/runs/{run_id}/sources/{source_key}/redistill`
- `POST /persona-factory/runs/{run_id}/review-actions`
- `PATCH /persona-factory/items/{item_id}`
- `POST /persona-factory/items/{item_id}/approve`
- `POST /persona-factory/items/{item_id}/reject`
- `POST /persona-factory/items/bulk-review`
- `POST /persona-factory/runs/{run_id}/items/bulk-review/preview`
- `POST /persona-factory/runs/{run_id}/items/bulk-review`
- `POST /persona-factory/runs/{run_id}/normalize`
- `POST /persona-factory/runs/{run_id}/synthesize-package`
- `PATCH /persona-factory/runs/{run_id}/package`
- `POST /persona-factory/runs/{run_id}/approve`
- `POST /persona-factory/runs/{run_id}/publish`

Workflow snapshot helpers:

- `GET /workflows/{workflow_id}/persona-version-notices`
- `POST /workflows/{workflow_id}/persona-agents/{agent_id}/use-latest`
- `POST /workflows/{workflow_id}/persona-agents/{agent_id}/keep-current`

## Configuration

Persona Factory run limits:

- `PERSONA_FACTORY_MAX_DOCUMENTS_PER_RUN`, default `25`
- `PERSONA_FACTORY_MAX_SOURCE_MEMORIES_PER_RUN`, default `250`
- `PERSONA_FACTORY_MAX_SOURCE_CHARACTERS_PER_RUN`, default `300000`
- `PERSONA_FACTORY_DEFAULT_DISTILLATION_MODE`, default `llm`
- `PERSONA_FACTORY_DEFAULT_LLM_MODEL_SOURCE`, default `main_agent`
- `PERSONA_FACTORY_LLM_DISTILLATION_ENABLED`, default `true`
- `PERSONA_FACTORY_HYBRID_DISTILLATION_ENABLED`, default `true`
- `PERSONA_FACTORY_LLM_MAX_SOURCE_MEMORIES_PER_RUN`, default `100`
- `PERSONA_FACTORY_LLM_MAX_SOURCE_CHARACTERS_PER_RUN`, default `120000`
- `PERSONA_FACTORY_LLM_MAX_SOURCE_TOKENS_PER_RUN`, default `30000`
- `PERSONA_FACTORY_LLM_MAX_CALLS_PER_RUN`, default `100`
- `PERSONA_FACTORY_LLM_TIMEOUT_SECONDS`, default `15`
- `PERSONA_FACTORY_LLM_RETRY_ATTEMPTS`, default `0`

Graph context/runtime flags:

- `GRAPH_PROJECTION_ENABLED`
- `AGENCY_GRAPH_CONTEXT_TOOLS_ENABLED`
- `GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED`

Use the run limits as backend safety rails. Large corpora should be processed in batches and synthesized into later versions.

## Tests And Smoke Validation

Focused backend tests:

```bash
pytest tests/test_persona_domain_repositories.py \
  tests/test_persona_distillation_pipeline.py \
  tests/test_persona_factory_api.py \
  tests/test_persona_package_synthesis.py \
  tests/test_workflow_persona_versions.py \
  tests/conversations/test_main_agent.py
```

Graph-related tests:

```bash
pytest tests/test_graph_parity.py tests/test_neo4j_graph_projection.py
```

API contract guard:

```bash
pytest tests/test_api_main.py
```

Backend smoke:

```bash
API=http://localhost:8000 bash scripts/persona_factory_smoke.sh
CLEANUP=1 API=http://localhost:8000 bash scripts/persona_factory_smoke.sh
```

Use `CLEANUP=1` for validation runs that should archive the generated Persona and delete smoke memory records. Omit cleanup when you need to inspect the Persona or Neo4j projection after publish.

LLM distillation rollout verification:

```bash
python -m unittest tests.test_persona_factory_api tests.test_persona_llm_distillation_schema tests.test_eval_runner tests.test_documentation_consistency
python scripts/run_evals.py --no-write --json
python scripts/run_evals.py --suite persona_distillation --case-id persona_messy_meeting_notes --live-persona-distillation --base-url http://localhost:8000 --live-llm-model-source main_agent --timeout-seconds 900 --no-write --json
python scripts/run_evals.py --suite persona_distillation --case-id persona_sop_policy --live-persona-distillation --base-url http://localhost:8000 --live-llm-model-source main_agent --timeout-seconds 900 --no-write --json
python scripts/run_evals.py --suite persona_distillation --case-id persona_tool_process --live-persona-distillation --base-url http://localhost:8000 --live-llm-model-source main_agent --timeout-seconds 900 --no-write --json
```

Latest rollout verification on 2026-06-02: `93` focused backend/doc tests passed, offline evals passed `12/12` with
average score `100.0`, and the three previously failing live Persona distillation cases listed above passed against a
running backend. The full live Persona suite is optional for local benchmarking; it can be slow because each case runs
multiple backend LLM distillation modes.

## Maintenance Rules

- Keep `Persona` as the canonical internal and API term.
- Mention skill-style artifacts only for import/export interoperability or developer explanation.
- Do not add `/skills`, `/skill-factory`, or `/personas` compatibility endpoints.
- Keep raw source uploads, source intelligence, distillation items, package versions, and published memory records traceable through source refs.
- Do not let graph projection failures fail primary Persona writes.
- Do not let runtime Graph RAG failure fail Persona invocation.
- Do not publish unreviewed personal, intimate, regulated, self, public-figure, or unverified-private-person items.
- Keep document ingestion and upload intelligence Agency-wide, not Persona-only.
- Prefer adding new distillers behind `PersonaDistillationItem` records rather than directly editing package generation.
- When changing package fields, update validation, synthesis, publishing, runtime prompt composition, tests, and frontend type contracts together.
- When a dedicated Persona Factory frontend package is added or identified, add component tests for mode/model selection,
  missing LLM metadata on legacy runs, review-summary filtering, and review actions.

## Documentation Ownership

Keep backend Persona developer guidance in this file. Keep stable operator steps in `docs/runbook.md`; keep
`docs/persona-factory-cli.md` only as the temporary curl appendix until its useful steps are folded into the platform
runbook. Keep API overview in `docs/frontend-api.md` as the consolidated frontend/backend contract document, with Persona
as one section rather than a separate API guide.
