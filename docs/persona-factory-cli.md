# Persona Factory CLI/API Workflow

Persona is Agency's canonical product and API term for a reusable identity/expertise package. Other platforms may call a similar package a "skill"; in Agency, a persona can include style, expertise, memory provenance, workflows, decision patterns, tools, examples, and guardrails.

Backend developer guidance lives in `docs/persona-factory.md`. Platform operator entry points live in `docs/runbook.md`.
This file is a curl-oriented appendix and should shrink over time as stable steps are folded into the platform runbook
rather than expanded into a second backend design source.

This workflow is backend-first. It does not require `open-agency-fe`.

## Conceptual Model

Persona Factory is built on Agency's existing memory and agent primitives:

- Memory is what is known.
- Agent is who acts at runtime.
- Persona is who or what the agent acts as.
- Persona Factory is how source material becomes a governed, reusable persona.

A persona is therefore not a replacement for memory or agents. It is a reviewed package of instructions, distilled memory,
style, decision patterns, workflows, optional tool bindings, provenance, governance labels, and version state. Publishing a
persona can materialize or bind an agent, while the persona remains the source of truth for the simulated identity or
expertise.

## Setup

Use trusted identity headers for local development, or create a bearer API token for headless usage. Persona Factory write calls require `personas:write`; catalog reads require `personas:read`. Memory listing requires `memory:read`, and manual memory/document creation requires `memory:write`.

```bash
API=http://localhost:8000
USER_ID=persona-user
USER_EMAIL=persona@example.com

curl -sS -X POST "$API/users/sync" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "persona-user",
    "email": "persona@example.com",
    "display_name": "Persona User"
  }'

AUTH=(-H "x-agency-user-id: $USER_ID" -H "x-agency-user-email: $USER_EMAIL")
JSON=(-H "Content-Type: application/json")
```

If `AGENCY_INTERNAL_API_KEY` is configured, also send `x-agency-internal-api-key` to `/users/sync`. For bearer-token automation, call `POST /api-tokens` as a real user and request `personas:read`, `personas:write`, `memory:read`, and `memory:write` as needed.

## Common Operations

- Start from uploaded files: `POST /documents/intelligence`, then `POST /documents/ingest`, then `POST /persona-factory/distill`.
- Resume backend work: `GET /persona-factory/runs`, then `GET /persona-factory/runs/{run_id}`.
- Review extracted records: `GET /persona-factory/runs/{run_id}/items`.
- Review source routing: `GET /persona-factory/runs/{run_id}/source-map`, then `GET /persona-factory/runs/{run_id}/sources/{source_key}`.
- Correct source routing: `PATCH /persona-factory/runs/{run_id}/sources/{source_key}/classification`.
- Rebuild one source after correction: `POST /persona-factory/runs/{run_id}/sources/{source_key}/redistill`.
- Publish the persona: approve items, `POST /persona-factory/runs/{run_id}/synthesize-package`, `POST /persona-factory/runs/{run_id}/approve`, then `POST /persona-factory/runs/{run_id}/publish`.

For a backend-only lifecycle smoke test against a local API, run:

```bash
API=http://localhost:8000 bash scripts/persona_factory_smoke.sh
```

The script creates one source memory, distills a persona, corrects source routing, re-distills that source, bulk-approves
reviewable items, synthesizes, approves, publishes, and reads graph context.

By default the smoke persona is preserved so you can inspect it in `/persona`, call
`GET /persona/{persona_id}/graph-context`, or wait for graph projection to catch up. For validation runs that should not
leave local smoke data behind, enable cleanup:

```bash
CLEANUP=1 API=http://localhost:8000 bash scripts/persona_factory_smoke.sh
```

Cleanup archives the generated persona and deletes the source memory plus published persona memory records after the
script prints its JSON result. If you need to inspect Neo4j projection for that exact persona after the run, omit
`CLEANUP=1`, run the graph projector catch-up command below, inspect the graph context, then archive/delete it manually.

## 1. Create Source Memory

Persona Factory distills from existing durable memory records. You can create memory directly:

```bash
curl -sS -X POST "$API/memories" \
  "${AUTH[@]}" "${JSON[@]}" \
  -d '{
    "memory": {
      "id": "audit-decision-1",
      "scope": "user",
      "content": "Audit observations should be graded by risk, evidence quality, and management impact.",
      "summary": "Audit observation grading rule",
      "memory_type": "decision",
      "tags": ["persona-source", "audit"],
      "importance": 80
    }
  }'

curl -sS -X POST "$API/memories" \
  "${AUTH[@]}" "${JSON[@]}" \
  -d '{
    "memory": {
      "id": "audit-workflow-1",
      "scope": "user",
      "content": "The audit review workflow starts with planning, then testing, issue validation, and MLP drafting.",
      "summary": "Audit review lifecycle",
      "memory_type": "fact",
      "tags": ["persona-source", "audit", "workflow"],
      "importance": 75
    }
  }'
```

Or ingest files into memory first:

```bash
curl -sS -X POST "$API/documents/intelligence" \
  "${AUTH[@]}" \
  -F "file=@./audit-sop.pdf" \
  -F "purpose=persona_factory" \
  -F "tags=persona-source,audit" \
  | jq '{kind: .document_kind, tags: .recommended.tags, chunk_size: .recommended.chunk_size, governance: .recommended.governance_labels}'

curl -sS -X POST "$API/documents/ingest" \
  "${AUTH[@]}" \
  -F "file=@./audit-sop.pdf" \
  -F "scope=user" \
  -F "tags=persona-source,audit" \
  -F "auto_intelligence=true" \
  -F "purpose=persona_factory"
```

The upload intelligence step uses the active main-agent model profile to recommend the document kind, tags, chunking,
and persona governance labels. The document response returns `memory_ids`; pass those IDs into distillation.

## 2. Distill A Persona Draft

`POST /persona-factory/distill` creates a persona if `persona_id` is omitted. Governance labels are part of the draft package and should be reviewed before publishing.

```bash
curl -sS -X POST "$API/persona-factory/distill" \
  "${AUTH[@]}" "${JSON[@]}" \
  -d '{
    "name": "Audit Manager Persona",
    "description": "Reviews audit evidence and drafts management-level observations.",
    "source_memory_ids": ["audit-decision-1", "audit-workflow-1"],
    "persona_type": "professional",
    "capability_mode": "persona_plus_expertise",
    "consent_status": "explicit_consent",
    "source_basis": "uploaded_private_material",
    "sensitivity_level": "standard",
    "visibility": "private"
  }' | tee distill.json

RUN_ID=$(jq -r '.run.id' distill.json)
PERSONA_ID=$(jq -r '.persona.id' distill.json)

jq '.items[] | {item_type, memory_layer, title, confidence, needs_review}' distill.json
```

Distillation mode is optional. If omitted, the backend uses `PERSONA_FACTORY_DEFAULT_DISTILLATION_MODE`, which defaults to
`llm`.

Deterministic mode runs the bounded local extraction pipeline:

```bash
curl -sS -X POST "$API/persona-factory/distill" \
  "${AUTH[@]}" "${JSON[@]}" \
  -d '{
    "name": "Audit Manager Deterministic",
    "source_memory_ids": ["audit-decision-1", "audit-workflow-1"],
    "distillation_mode": "deterministic"
  }'
```

LLM mode uses LLM distillers as the main extraction path. By default it resolves the active main-agent model profile:

```bash
curl -sS -X POST "$API/persona-factory/distill" \
  "${AUTH[@]}" "${JSON[@]}" \
  -d '{
    "name": "Audit Manager LLM",
    "source_memory_ids": ["audit-decision-1", "audit-workflow-1"],
    "distillation_mode": "llm",
    "llm_model_source": "main_agent"
  }'
```

To use a specific model profile, send either the explicit source or the legacy-compatible `model_profile_id` shape:

```bash
curl -sS -X POST "$API/persona-factory/distill" \
  "${AUTH[@]}" "${JSON[@]}" \
  -d '{
    "name": "Audit Manager LLM Profile",
    "source_memory_ids": ["audit-decision-1", "audit-workflow-1"],
    "distillation_mode": "llm",
    "llm_model_source": "model_profile",
    "model_profile_id": "my-structured-model"
  }'
```

Hybrid mode runs deterministic and LLM extraction, then merges duplicate, complementary, and conflicting candidates into a
reviewable item set:

```bash
curl -sS -X POST "$API/persona-factory/distill" \
  "${AUTH[@]}" "${JSON[@]}" \
  -d '{
    "name": "Audit Manager Hybrid",
    "source_memory_ids": ["audit-decision-1", "audit-workflow-1"],
    "distillation_mode": "hybrid"
  }'
```

The deterministic pipeline classifies each selected memory chunk, runs specialized extractors, normalizes duplicates,
validates review state, and stores small structured items. One source memory can produce multiple records, such as
`workflow`, `decision_pattern`, `tool_usage`, `writing_style`, `guardrail`, and `example`. LLM and hybrid modes write the
same item contract, with extra metadata for model provenance, evidence grounding, extraction source, review flags,
distillers, and conflict groups.

Persona Factory reuses document intelligence produced during ingestion or memory review. It first honors stored
`metadata.source_intelligence.classification`, then falls back to `metadata.upload_intelligence`, then to deterministic
source heuristics. Extracted items keep the distiller name/version, routing payload, source classification, and source refs
so reviewers can see why a chunk became knowledge, workflow, decision, guardrail, tool, style, or example memory.

If `model_profile_id` is supplied and source intelligence is not already present, Persona Factory uses that model profile
for optional structured source classification before deterministic extraction. Model responses are schema-validated before
any item is persisted; invalid labels or malformed structured responses fail the run instead of falling back silently.

The model classifier returns source intelligence for each selected chunk:

- primary source classification, such as `policy_sop`, `decision`, `workflow`, `tool_usage`, or `domain_knowledge`
- document kind, such as `workpaper`, `chat_export`, `ticket`, `meeting_note`, or `policy_sop`
- content roles, extraction targets, and memory layers
- vector tags for filtered semantic retrieval
- graph entity and relationship hints for Neo4j projection and future Graph RAG
- include/exclude recommendation and rationale

Persona Factory enforces safe per-run limits before creating a persona. Defaults are:

- `PERSONA_FACTORY_MAX_DOCUMENTS_PER_RUN=25`
- `PERSONA_FACTORY_MAX_SOURCE_MEMORIES_PER_RUN=250`
- `PERSONA_FACTORY_MAX_SOURCE_CHARACTERS_PER_RUN=300000`
- `PERSONA_FACTORY_DEFAULT_DISTILLATION_MODE=llm`
- `PERSONA_FACTORY_DEFAULT_LLM_MODEL_SOURCE=main_agent`
- `PERSONA_FACTORY_LLM_DISTILLATION_ENABLED=true`
- `PERSONA_FACTORY_HYBRID_DISTILLATION_ENABLED=true`
- `PERSONA_FACTORY_LLM_MAX_SOURCE_MEMORIES_PER_RUN=100`
- `PERSONA_FACTORY_LLM_MAX_SOURCE_CHARACTERS_PER_RUN=120000`
- `PERSONA_FACTORY_LLM_MAX_SOURCE_TOKENS_PER_RUN=30000`
- `PERSONA_FACTORY_LLM_MAX_CALLS_PER_RUN=100`
- `PERSONA_FACTORY_LLM_TIMEOUT_SECONDS=15`
- `PERSONA_FACTORY_LLM_RETRY_ATTEMPTS=0`

Large source sets should be split into multiple distillation runs, reviewed in batches, and synthesized into later
persona versions.

For personal or entertainment personas, use governance labels such as `persona_type: "personal"`, `capability_mode: "persona_only"`, `source_basis: "chat_export"`, `sensitivity_level: "intimate"`, and `visibility: "private"` when appropriate.
Generated packages include default governance guardrails for simulated personas, including no claim to be the actual
person. Personal, self, and public-figure personas also receive defaults against unsupported private facts and
overconfident responses when source support is weak.
Identity-like source claims, such as family relationships, intimate relationships, self-representation, or private-person
simulation language, are forced to `needs_review` even when the source classification is otherwise high confidence.

Use the catalog endpoint when building CLI prompts or UI controls:

```bash
curl -sS "$API/persona-factory/governance-labels" "${AUTH[@]}" \
  | jq '{defaults, allowed_values, validation_rules}'
```

Governance validation is conservative. Personal personas require explicit, self, or unverified-private-person consent.
Unverified private-person personas must remain private. Marketplace personas must use standard sensitivity and cannot be
based only on private memory records, uploaded private material, or chat exports. Public-figure marketplace personas must
be based on public sources with `public_material` consent.

Persona Factory lifecycle actions emit `persona` graph projection/audit events for distillation, item edits, item review,
normalization, package synthesis, package approval, publishing, and runtime invocation.

## 3. Review And Edit The Package

The distillation response includes reviewable `items` plus `run.output_package`. Use the run and item APIs to recover
state, edit extracted items, and approve or reject individual records. The initial package is a preview from source
memory; use `synthesize-package` after item review to rebuild the package from active structured items.

```bash
curl -sS "$API/persona-factory/runs/$RUN_ID" "${AUTH[@]}" \
  | jq '{run: .run.id, item_count: (.items | length)}'

curl -sS "$API/persona-factory/runs?created_by_user_id=$USER_ID" "${AUTH[@]}" \
  | jq '.items[] | {id, persona_id, status}'

curl -sS "$API/persona-factory/runs/$RUN_ID/items" "${AUTH[@]}" \
  | jq '{total, filtered_count, limit, offset, items: [.items[] | {id, item_type, review_status, title}]}'

curl -sS "$API/persona-factory/runs/$RUN_ID/items?item_type=decision_pattern&needs_review=true&limit=50&offset=0" \
  "${AUTH[@]}" \
  | jq '{filtered_count, counts, items: [.items[] | {id, title, confidence, source: .source_memory_id}]}'

curl -sS "$API/persona-factory/runs/$RUN_ID/source-map" "${AUTH[@]}" \
  | jq '.items[] | {label, classification, document_kind, item_count, needs_review_count, distillers, vector_tags}'

curl -sS "$API/persona-factory/runs/$RUN_ID/review-summary" "${AUTH[@]}" \
  | jq '{mode: .distillation_mode, counts, extraction_sources, review_flags, conflict_groups}'

curl -sS "$API/persona-factory/runs/$RUN_ID/items?extraction_source=llm&review_flag=material_conflict" \
  "${AUTH[@]}" \
  | jq '{filtered_count, items: [.items[] | {id, title, distillers: .review_metadata.distillers, flags: .review_metadata.review_flags}]}'

SOURCE_KEY=$(curl -sS "$API/persona-factory/runs/$RUN_ID/source-map" "${AUTH[@]}" \
  | jq -r '.items[0].key')

curl -sS "$API/persona-factory/runs/$RUN_ID/sources/$SOURCE_KEY?limit=10" "${AUTH[@]}" \
  | jq '{source: .source.label, counts, items: [.items[] | {id, item_type, review_status, title}]}'

curl -sS -X PATCH "$API/persona-factory/runs/$RUN_ID/sources/$SOURCE_KEY/classification" \
  "${AUTH[@]}" "${JSON[@]}" \
  -d '{
    "classification": "workflow",
    "document_kind": "ticket",
    "content_roles": ["workflow"],
    "extraction_targets": ["workflow", "decision_pattern"],
    "memory_layers": ["procedural"],
    "vector_tags": ["release", "manual-flow"],
    "confidence": 0.97,
    "rationale": "Reviewer corrected the source routing before re-distillation."
  }' \
  | jq '{source_key, classification, updated_memory_ids, updated_item_count}'

curl -sS -X POST "$API/persona-factory/runs/$RUN_ID/sources/$SOURCE_KEY/redistill" \
  "${AUTH[@]}" "${JSON[@]}" \
  -d '{"limit": 250}' \
  | jq '{source_key, superseded_count, created_count, items: [.items[] | {id, item_type, review_status, title}]}'

CONFLICT_GROUP_ID=$(curl -sS "$API/persona-factory/runs/$RUN_ID/review-summary" "${AUTH[@]}" \
  | jq -r '.conflict_groups[0].id // empty')

if [ -n "$CONFLICT_GROUP_ID" ]; then
  curl -sS -X POST "$API/persona-factory/runs/$RUN_ID/review-actions" \
    "${AUTH[@]}" "${JSON[@]}" \
    -d "{
      \"action\": \"prefer_llm\",
      \"conflict_group_id\": \"$CONFLICT_GROUP_ID\",
      \"reason\": \"The LLM item keeps better source-backed wording after reviewer comparison.\"
    }" \
    | jq '{action, updated_count, items: [.items[] | {id, review_status, title}]}'
fi

ITEM_ID=$(curl -sS "$API/persona-factory/runs/$RUN_ID/items" "${AUTH[@]}" \
  | jq -r '.items[0].id')

curl -sS -X PATCH "$API/persona-factory/items/$ITEM_ID" \
  "${AUTH[@]}" "${JSON[@]}" \
  -d '{
    "patch": {
      "title": "Reviewed audit severity rule",
      "confidence": 0.9,
      "review_status": "draft",
      "needs_review": false
    }
  }'

curl -sS -X POST "$API/persona-factory/items/$ITEM_ID/approve" \
  "${AUTH[@]}"

ITEM_IDS=$(curl -sS "$API/persona-factory/runs/$RUN_ID/items" "${AUTH[@]}" \
  | jq '[.items[] | select(.review_status != "approved" and .review_status != "rejected") | .id]')

curl -sS -X POST "$API/persona-factory/items/bulk-review" \
  "${AUTH[@]}" "${JSON[@]}" \
  -d "{\"action\": \"approve\", \"item_ids\": $ITEM_IDS}" \
  | jq '{action, count}'

curl -sS -X POST "$API/persona-factory/runs/$RUN_ID/items/bulk-review/preview" \
  "${AUTH[@]}" "${JSON[@]}" \
  -d '{
    "action": "approve",
    "filters": {
      "source_key": "doc-intel",
      "item_type": "decision_pattern",
      "review_status": "draft",
      "min_confidence": 0.7
    },
    "limit": 250
  }' \
  | jq '{action, count, matched_count, reviewable_count, has_more, sample: [.items[] | {id, title}]}'

curl -sS -X POST "$API/persona-factory/runs/$RUN_ID/items/bulk-review" \
  "${AUTH[@]}" "${JSON[@]}" \
  -d '{
    "action": "approve",
    "filters": {
      "source_key": "doc-intel",
      "item_type": "decision_pattern",
      "review_status": "draft",
      "min_confidence": 0.7
    },
    "limit": 250
  }' \
  | jq '{action, count, matched_count, reviewable_count, has_more}'
```

Normalize after extraction or manual edits when sources may contain repeated claims. Normalization merges duplicate active
items, carries all source references into the surviving item, marks merged duplicates as `superseded`, and flags simple
conflicts as `needs_review`.
For runs created with `model_profile_id`, normalization also asks the model for strictly schema-validated updates,
duplicate marks, and conflict groups after deterministic normalization. Any model-suggested content or title changes are
forced back to `needs_review` before they can be published.

```bash
curl -sS -X POST "$API/persona-factory/runs/$RUN_ID/normalize" \
  "${AUTH[@]}" | tee normalized.json

jq '.normalization' normalized.json
```

After item review and optional normalization, synthesize the package from active items. Rejected and superseded items are excluded.

```bash
curl -sS -X POST "$API/persona-factory/runs/$RUN_ID/synthesize-package" \
  "${AUTH[@]}" | tee synthesized.json

jq '.run.output_package.provenance | {strategy, distillation_item_ids, excluded_item_ids}' synthesized.json
```

Then save the package, edit it, and patch the run if final manual edits are needed.

```bash
jq '.run.output_package' distill.json > package.json

curl -sS -X PATCH "$API/persona-factory/runs/$RUN_ID/package" \
  "${AUTH[@]}" "${JSON[@]}" \
  -d "$(jq -n --slurpfile package package.json '{package: $package[0]}')" \
  | tee reviewed-run.json
```

The package must keep `schema_version: 1`, a `persona` object, and `memory_layers`. Typical editable sections are:

- `persona`: summary, communication style, preferences, escalation style, response style.
- `knowledge`: facts and domain expertise with source references.
- `decision_patterns`: prioritization, risk grading, approval thresholds, and tradeoffs.
- `workflows`: repeatable procedures or lifecycle steps.
- `tools`: proposed tool bindings; only granted tools become agent tool IDs.
- `guardrails`: source-backed limits, safety rules, or escalation rules.
- `examples`: sample interactions or artifacts.
- `governance`: persona type, consent/source basis, sensitivity, and visibility.

## 4. Approve And Publish

Approval freezes the reviewed package into a version. Publishing marks it active, materializes an `AgentDefinition`, and writes persona-scoped memory with provenance.
When the package was synthesized from reviewed items, only approved item entries are written as persona-scoped memory;
rejected or superseded items remain in the run for audit but are not injected at runtime.

```bash
curl -sS -X POST "$API/persona-factory/runs/$RUN_ID/approve" \
  "${AUTH[@]}" "${JSON[@]}" \
  -d '{"version": "1.0.0"}' | tee approved.json

curl -sS -X POST "$API/persona-factory/runs/$RUN_ID/publish" \
  "${AUTH[@]}" | tee published.json
```

Verify the catalog:

```bash
curl -sS "$API/persona" "${AUTH[@]}" | jq '.items[] | {id, slug, status, published_agent_id}'

curl -sS "$API/persona/$PERSONA_ID/versions" "${AUTH[@]}" \
  | jq '.items[] | {id, version, status, published_at}'

curl -sS "$API/persona/$PERSONA_ID/sources" "${AUTH[@]}" \
  | jq '.items[] | {source_type, source_id, filename, storage_uri}'
```

## 5. Use Published Personas In Workflows

Publishing a persona materializes a canonical `AgentDefinition`, but workflows embed a copy of that agent under
`agent_definitions`. The embedded copy is the workflow runtime snapshot. It does not silently change when a newer persona
version is published.

To find workflows that use a persona:

```bash
curl -sS "$API/persona/$PERSONA_ID/workflow-usages" "${AUTH[@]}" \
  | jq '.items[] | {workflow_id, workflow_name, agent_id, status, persona_version, current_persona_version}'
```

Usage `status` values are:

- `current`: the workflow snapshot uses the persona's current published version.
- `outdated`: the workflow snapshot uses an older persona version and can be refreshed.
- `pinned`: the operator intentionally kept the older workflow snapshot for the current published persona version.

To show only actionable notices for one workflow:

```bash
WORKFLOW_ID=workflow-persona-demo

curl -sS "$API/workflows/$WORKFLOW_ID/persona-version-notices" "${AUTH[@]}" \
  | jq '.items[] | {agent_id, persona_slug, status, message, actions}'
```

To update one embedded persona-backed workflow agent to the latest published persona agent:

```bash
AGENT_ID=$(curl -sS "$API/workflows/$WORKFLOW_ID/persona-version-notices" "${AUTH[@]}" \
  | jq -r '.items[0].agent_id')

curl -sS -X POST "$API/workflows/$WORKFLOW_ID/persona-agents/$AGENT_ID/use-latest" \
  "${AUTH[@]}" | jq '{agent: .agent.id, notices: .persona_version_notices}'
```

`use-latest` replaces the embedded workflow agent with the currently published persona agent and preserves
workflow-local graph metadata such as `workflow_graph_position`.

To keep the workflow snapshot for now:

```bash
curl -sS -X POST "$API/workflows/$WORKFLOW_ID/persona-agents/$AGENT_ID/keep-current" \
  "${AUTH[@]}" | jq '.usage | {agent_id, status, persona_version, current_persona_version}'
```

`keep-current` records the current persona version in agent metadata. The notice stays quiet until a newer persona
version is published after that decision.

## 6. Capture Feedback For Continuous Learning

Feedback and accepted edits do not silently change a published persona. They create a new `needs_review` distillation
run, a source memory with `source: "persona_feedback"`, and a reviewable item. Approve the item, synthesize the package,
approve the new run, and publish it to create the next version.

```bash
curl -sS -X POST "$API/persona-factory/feedback" \
  "${AUTH[@]}" "${JSON[@]}" \
  -d '{
    "persona_id": "'"$PERSONA_ID"'",
    "title": "Tighter audit severity rule",
    "content": "Accepted correction: escalate when privileged access review excludes vendor or administrator accounts.",
    "item_type": "decision_pattern",
    "memory_layer": "procedural",
    "feedback_type": "accepted_edit",
    "source_conversation_id": "conversation-feedback-1",
    "source_message_id": "message-feedback-1"
  }' | tee feedback.json

FEEDBACK_RUN_ID=$(jq -r '.run.id' feedback.json)
FEEDBACK_ITEM_ID=$(jq -r '.items[0].id' feedback.json)

curl -sS -X POST "$API/persona-factory/runs/$FEEDBACK_RUN_ID/synthesize-package" "${AUTH[@]}"

# This remains blocked until the feedback item is approved.
curl -sS -X POST "$API/persona-factory/runs/$FEEDBACK_RUN_ID/approve" \
  "${AUTH[@]}" "${JSON[@]}" -d '{}'

curl -sS -X POST "$API/persona-factory/items/$FEEDBACK_ITEM_ID/approve" "${AUTH[@]}"
curl -sS -X POST "$API/persona-factory/runs/$FEEDBACK_RUN_ID/synthesize-package" "${AUTH[@]}"
curl -sS -X POST "$API/persona-factory/runs/$FEEDBACK_RUN_ID/approve" \
  "${AUTH[@]}" "${JSON[@]}" -d '{}' | tee feedback-approved.json
curl -sS -X POST "$API/persona-factory/runs/$FEEDBACK_RUN_ID/publish" "${AUTH[@]}"
```

When no explicit version is supplied during approval, Persona Factory increments the patch label, for example from
`1.0.0` to `1.0.1`.

Rollback republishes an earlier approved or published version:

```bash
ROLLBACK_VERSION_ID=$(jq -r '.persona_version.id' published.json)

curl -sS -X POST "$API/persona/$PERSONA_ID/versions/$ROLLBACK_VERSION_ID/rollback" \
  "${AUTH[@]}" | jq '.rollback'
```

## 7. Export Explicitly

Persona is the internal source of truth. Export is explicit and does not create `/skills` CRUD endpoints.

Canonical JSON export:

```bash
curl -sS "$API/persona/$PERSONA_ID/export?format=json" "${AUTH[@]}" \
  | jq '{export_type, persona: .persona.slug, version: .persona_version.version, strategy: .package.provenance.strategy}'
```

Skill-style interoperability export:

```bash
curl -sS "$API/persona/$PERSONA_ID/export?format=skill_markdown" "${AUTH[@]}" \
  | jq '.files | keys'
```

The skill-style export includes `skill.md`, `persona.md`, `workflow.md`, `decision_patterns.md`, `tools.yaml`,
`guardrails.md`, and `examples.md`. These files are for interoperability with ecosystems that use skill terminology;
Agency still stores and runs the package as a Persona.

Skill-style import is also explicit:

```bash
curl -sS -X POST "$API/persona/import" \
  "${AUTH[@]}" "${JSON[@]}" \
  -d '{
    "name": "Imported Audit Persona",
    "format": "skill_markdown",
    "files": {
      "skill.md": "# Imported Audit Persona\n\nAgency Persona export.",
      "persona.md": "# Persona\n\nReviews audit evidence in a concise style.",
      "decision_patterns.md": "# Decision Patterns\n\n## Audit Severity\n\nEscalate when privileged access review excludes administrators.",
      "workflow.md": "# Workflows\n\n## Audit Review\n\nPlan, test, validate issues, then draft the MLP observation.",
      "tools.yaml": "tools:\n  - name: \"Jira\"\n    tool_id: \"jira\"\n    granted: false\n",
      "guardrails.md": "# Guardrails\n\n## Evidence\n\nDo not invent missing evidence.",
      "examples.md": "# Examples\n\n## Observation\n\nAccess review scope excluded administrators."
    }
  }'
```

Imports create a draft Persona version using `skill-style-import-v1` provenance. Review and publish it through the normal
Persona Factory lifecycle before runtime use.

## 8. Invoke At Runtime

Mention the published persona slug with `@slug` in a conversation message. Runtime loads the published persona package, persona-scoped memory, and generated agent instructions before asking the model to answer.
Unknown mentions return a `persona_error: "not_found"` assistant message. Existing personas that are still draft,
in review, approved-but-unpublished, or archived return `persona_error: "not_published"`; runtime loads only published
persona versions by default.
Use `@slug:version` to target a specific published persona version by version label or version id, for example
`@audit-manager-persona:1.0.0`. If the requested version is not published or does not exist, runtime returns
`persona_error: "version_not_found"`.
Persona assistant messages include `metadata.persona_provenance` with package strategy, distillation run, source memory
IDs, distillation item IDs, source references, and runtime context trace data for audit/debug. The runtime context trace
shows whether the answer prompt used approved persona memory, raw source-memory fallback, graph context, or a combination.
Persona package runtime settings may include `memory_layer_filter`, such as `["semantic", "tool"]`, to restrict which
persona-scoped memory layers can be injected at invocation time.

Conversation runtime requires an active main-agent profile/model setup.

```bash
curl -sS -X POST "$API/conversations" \
  "${AUTH[@]}" \
  "${JSON[@]}" \
  -d '{
    "id": "conversation-persona-demo",
    "created_by_user_id": "persona-user",
    "channel_type": "api"
  }'

curl -sS -X POST "$API/conversations/conversation-persona-demo/messages" \
  "${AUTH[@]}" \
  "${JSON[@]}" \
  -d '{
    "message": {
      "role": "user",
      "message_type": "user_text",
      "plain_text": "@audit-manager-persona review this observation draft",
      "content": {
        "text": "@audit-manager-persona review this observation draft"
      }
    },
    "response_mode": "sync"
  }' | tee persona-response.json

jq '{persona, assistant_message: .assistant_message.plain_text}' persona-response.json
```

A successful persona-routed response includes `persona.slug` and assistant message metadata with `delivery: "persona"`, `persona_id`, `persona_slug`, and `persona_version_id`.

## Graph Projection

Persona Factory lifecycle actions emit graph projection events for Agency Brain and the realtime graph stream. The projection models `Persona`, `PersonaVersion`, `DistillationRun`, `DistillationItem`, and `SourceMemory`, plus package-level `Tool`, `Workflow`, and `Artifact` nodes when reviewed items synthesize those sections.

Key relationships include `PERSONA_HAS_VERSION`, `PERSONA_HAS_DISTILLATION_RUN`, `RUN_EXTRACTED_ITEM`, `ITEM_DERIVED_FROM_MEMORY`, `PERSONA_USES_TOOL`, `PERSONA_FOLLOWS_WORKFLOW`, and `PERSONA_PRODUCES_ARTIFACT`. Runtime invocation also projects the published persona to its generated agent and conversation context.

Graph read presets are available through `GET /graph/read/presets/{preset}` with `preset=persona_lineage` or `preset=persona_capability_map`.

Persona runtime uses a fixed Graph RAG policy by default:

- Runtime invocation preset: `persona_lineage`
- Runtime limit: `24` nodes/edges
- Source priority: persona package, approved persona memory, persona graph context, then conversation context
- Fallback: skip graph context without failing the persona invocation when Neo4j is disabled, unavailable, or empty
- Conflict rule: if graph context conflicts with approved persona memory or the published package, prefer the approved memory/package

The `persona_capability_map` preset is intended for inspection and UI/CLI explainability. Use it when you want to show what the persona can use, follow, or produce; keep `persona_lineage` as the runtime default because it preserves source lineage and reviewed-memory provenance.

Inspect the graph context that runtime persona invocation can use:

```bash
curl -sS "$API/persona/$PERSONA_ID/graph-context?limit=24&preset=persona_lineage" "${AUTH[@]}" \
  | jq '{persona: .persona.slug, policy: .policy, prompt: .prompt, nodes: (.graph.nodes | length), edges: (.graph.edges | length)}'
```

Inspect the persona capability map:

```bash
curl -sS "$API/persona/$PERSONA_ID/graph-context?limit=24&preset=persona_capability_map" "${AUTH[@]}" \
  | jq '{persona: .persona.slug, policy: .policy, nodes: (.graph.nodes | length), edges: (.graph.edges | length)}'
```

Freshly published personas can return an available but empty graph context until pending projection events are processed.
For local validation, run the default projector service, or run one manual projection pass and repeat the inspection:

```bash
NEO4J_ENABLED=true GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED=true docker compose up -d graph-projector
NEO4J_ENABLED=true ./.venv/bin/python -m app.cli graph-projection project-neo4j --ensure-schema --json
```

Recommended local graph smoke sequence:

```bash
API=http://localhost:8000 RUN_SUFFIX="$(date +%s)-graph" bash scripts/persona_factory_smoke.sh
NEO4J_ENABLED=true ./.venv/bin/python -m app.cli graph-projection project-neo4j --ensure-schema --json
PERSONA_ID="paste-persona-id-from-smoke-json"
curl -sS "$API/persona/$PERSONA_ID/graph-context?limit=24&preset=persona_lineage" \
  -H "x-agency-user-id: persona-smoke-user" \
  -H "x-agency-user-email: persona-smoke@example.com" \
  | jq '{persona: .persona.slug, policy: .policy.preset, nodes: (.graph.nodes | length), edges: (.graph.edges | length)}'
```

The smoke script prints `persona_id` in its JSON output. Use that value as `PERSONA_ID` for the follow-up graph-context
inspection.

To enqueue projection events for older approved source-intelligence graph hints, run:

```bash
./.venv/bin/python -m app.cli graph-projection backfill --domain source_intelligence_graph_hints --json
```

The graph-hint backfill uses stable source event ids, so re-running it does not duplicate unchanged approved hints.

## Governance Checklist

Before publishing, reviewers should confirm:

- Every material memory item has source provenance and confidence.
- Item-synthesized packages have no active `needs_review` items.
- Personal, intimate, regulated, self, public-figure, or unverified-private-person personas have every active item explicitly approved.
- `persona_type` and `capability_mode` match the intended use.
- Consent and source basis are explicit for personal, intimate, or identity-like personas.
- Sensitive or private personas use restrictive visibility.
- Tool grants are deliberate; proposed tools are not silently granted.
- Guardrails say the persona is simulated from provided source material and should not pretend to be the actual person.

## Canonical Endpoints

The canonical Persona APIs are:

- `GET /persona`
- `POST /persona`
- `GET /persona/{persona_id}`
- `PATCH /persona/{persona_id}`
- `DELETE /persona/{persona_id}`
- `GET /persona/{persona_id}/versions`
- `POST /persona/{persona_id}/versions/{version_id}/rollback`
- `GET /persona/{persona_id}/graph-context`
- `GET /persona/{persona_id}/workflow-usages`
- `GET /persona/{persona_id}/sources`
- `POST /persona/{persona_id}/sources`
- `GET /persona-factory/governance-labels`
- `GET /persona-factory/item-types`
- `POST /persona-factory/distill`
- `POST /persona-factory/feedback`
- `GET /persona-factory/runs`
- `GET /persona-factory/runs/{run_id}`
- `GET /persona-factory/runs/{run_id}/items`
  - Query filters: `source_key`, `item_type`, `memory_layer`, `review_status`, `needs_review`, `min_confidence`, `max_confidence`, `limit`, `offset`.
- `GET /persona-factory/runs/{run_id}/source-map`
- `GET /persona-factory/runs/{run_id}/sources/{source_key}`
- `PATCH /persona-factory/runs/{run_id}/sources/{source_key}/classification`
- `POST /persona-factory/runs/{run_id}/sources/{source_key}/redistill`
- `PATCH /persona-factory/items/{item_id}`
- `POST /persona-factory/items/{item_id}/approve`
- `POST /persona-factory/items/{item_id}/reject`
- `POST /persona-factory/items/bulk-review`
- `POST /persona-factory/runs/{run_id}/items/bulk-review/preview`
- `POST /persona-factory/runs/{run_id}/items/bulk-review`
- `POST /persona-factory/runs/{run_id}/synthesize-package`
- `PATCH /persona-factory/runs/{run_id}/package`
- `POST /persona-factory/runs/{run_id}/approve`
- `POST /persona-factory/runs/{run_id}/publish`
- `GET /workflows/{workflow_id}/persona-version-notices`
- `POST /workflows/{workflow_id}/persona-agents/{agent_id}/use-latest`
- `POST /workflows/{workflow_id}/persona-agents/{agent_id}/keep-current`

Do not add `/skills`, `/skill-factory`, or `/personas` compatibility endpoints. If Agency later imports or exports external skill-style packages, that should be an explicit adapter around the canonical Persona model.
