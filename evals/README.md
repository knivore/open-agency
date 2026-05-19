# Agency Evals

Deterministic eval cases live under `evals/cases` and run with:

```bash
make eval
```

The default suite is intended to be CI-safe: no live backend, no provider credentials, and no LLM calls. Cases use
fixture evidence to exercise the same assertion engine used for backend-backed runs.

Backend-backed cases can use `mode: existing_execution` or `mode: http_workflow`, but should be run explicitly with:

```bash
./.venv/bin/python scripts/run_evals.py --cases path/to/case.yaml --base-url http://localhost:8000
```

Add `--judge-agent` when you want the configured `Evaluation` agent to inspect the target run and produce a semantic
rubric verdict in addition to deterministic assertions.
