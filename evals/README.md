# Agency Evals

Deterministic eval cases live under `evals/cases` and run with:

```bash
make eval
```

The default suite is intended to be CI-safe: no live backend, no provider credentials, and no LLM calls. Cases use
fixture evidence to exercise the same assertion engine used for backend-backed runs.

Persona Factory extraction-quality evals use `mode: persona_distillation_fixture`. These compare deterministic, LLM,
and hybrid fixture outputs against expected persona items and report precision, recall, evidence grounding, item type
accuracy, memory layer accuracy, conflict detection, review burden, cost, and latency. The default cases remain mocked
and CI-safe. Persona fixtures may also include `persona_distillation.source_memories`; those sources are ignored by the
default mocked run and are used only by the explicit live-model replay mode.

Live Persona Factory extraction evals require a running backend and a configured structured-output model for `llm` and
`hybrid` modes. They create temporary user-scoped memories, run `/persona-factory/distill`, and score the returned draft
review items without approving or publishing personas:

```bash
./.venv/bin/python scripts/run_evals.py \
  --suite persona_distillation \
  --live-persona-distillation \
  --base-url http://localhost:8000 \
  --live-llm-model-source main_agent \
  --no-write \
  --json
```

Use `--live-persona-modes deterministic,llm,hybrid` to choose comparison modes. For explicit model selection, use
`--live-llm-model-source model_profile --live-model-profile-id <profile-id>` or
`--live-llm-model-source model --live-llm-model-provider <provider> --live-llm-model <model>`.

Persona runtime-impact evals use `mode: persona_runtime_fixture`. These compare published persona-version fixtures by
distillation mode against the same benchmark questions and score answer correctness, style fidelity, source grounding,
uncertainty/refusal behavior, graph-context usefulness, and the winning mode for each fixture type.

Latest fixture verification for the LLM distillation rollout:

- Date: 2026-06-02
- Command: `python scripts/run_evals.py --no-write --json`
- Result: 12/12 passed, average score `100.0`
- Covered suites: smoke runtime fixtures, `persona_distillation` deterministic/LLM/hybrid extraction fixtures, and
  `persona_runtime` published-persona behavior fixtures.

Latest focused live Persona distillation verification:

- Date: 2026-06-02
- Backend: local `http://localhost:8000`
- Model source: `main_agent`
- Passed cases: `persona_messy_meeting_notes`, `persona_sop_policy`, and `persona_tool_process`
- Note: the full live suite is intended for manual benchmarking and can be slow because each case runs multiple backend
  distillation modes with live LLM calls.

Backend-backed cases can use `mode: existing_execution` or `mode: http_workflow`, but should be run explicitly with:

```bash
./.venv/bin/python scripts/run_evals.py --cases path/to/case.yaml --base-url http://localhost:8000
```

Add `--judge-agent` when you want the configured `Evaluation` agent to inspect the target run and produce a semantic
rubric verdict in addition to deterministic assertions.
