from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts.run_evals import (
    DEFAULT_CASES_PATH,
    EvalResult,
    AssertionResult,
    LivePersonaDistillationOptions,
    evaluate_persona_distillation_case,
    evaluate_persona_runtime_case,
    filter_cases,
    load_eval_cases,
    run_live_persona_distillation_case,
    run_case,
    summarize_results,
    write_results,
    _live_persona_item,
)


class EvalRunnerTests(unittest.TestCase):
    def test_loads_default_smoke_case_and_runs_fixture_eval(self) -> None:
        cases = load_eval_cases(DEFAULT_CASES_PATH)
        selected = filter_cases(cases, suite="smoke", case_id="smoke_execution_trace")

        self.assertEqual(len(selected), 1)
        result = run_case(selected[0])

        self.assertTrue(result.passed)
        self.assertEqual(result.score, 100.0)
        self.assertEqual(result.execution_id, "execution-smoke-eval")
        self.assertTrue(all(assertion.passed for assertion in result.assertion_results))

    def test_default_eval_cases_are_fixture_backed_and_passing(self) -> None:
        cases = load_eval_cases(DEFAULT_CASES_PATH)

        self.assertGreaterEqual(len(cases), 3)
        self.assertTrue(all(
            case.get("mode") in {"fixture", "persona_distillation_fixture", "persona_runtime_fixture"}
            for case in cases
        ))
        results = [run_case(case) for case in cases]

        self.assertTrue(all(result.passed for result in results))
        self.assertEqual(sum(result.score for result in results) / len(results), 100.0)

    def test_fixture_eval_reports_failed_assertions_and_score(self) -> None:
        case = {
            "id": "fixture-failure",
            "suite": "unit",
            "mode": "fixture",
            "evidence": {
                "execution": {
                    "id": "execution-failure",
                    "status": "failed",
                    "output_payload": {"final_output": "actual output"},
                },
                "events": [],
                "artifacts": [],
            },
            "assertions": [
                {"type": "status_completed"},
                {"type": "final_output_contains", "value": "expected output"},
            ],
        }

        result = run_case(case)

        self.assertFalse(result.passed)
        self.assertEqual(result.score, 0.0)
        self.assertEqual([item.passed for item in result.assertion_results], [False, False])
        self.assertTrue(all(item.message.startswith("Failed:") for item in result.assertion_results))

    def test_collects_partial_score_for_mixed_assertions(self) -> None:
        case = {
            "id": "fixture-partial",
            "suite": "unit",
            "mode": "fixture",
            "evidence": {
                "execution": {
                    "id": "execution-partial",
                    "status": "completed",
                    "output_payload": {"result": {"items": [{"name": "alpha"}]}},
                },
                "events": [
                    {
                        "sequence": 1,
                        "event_type": "tool.call.completed",
                        "payload": {"tool_id": "agency.workflow.get"},
                        "metrics": {},
                    },
                    {
                        "sequence": 2,
                        "event_type": "llm.response.created",
                        "payload": {"usage": {"total_tokens": 25}},
                        "metrics": {},
                    },
                ],
                "artifacts": [],
            },
            "assertions": [
                {"type": "status_completed"},
                {"type": "output_path_equals", "path": "result.items.0.name", "value": "alpha"},
                {"type": "tool_not_called", "tool_id": "agency.workflow.get"},
                {"type": "max_total_tokens", "value": 50},
            ],
        }

        result = run_case(case)

        self.assertFalse(result.passed)
        self.assertEqual(result.score, 75.0)

    def test_persona_distillation_fixture_scores_modes(self) -> None:
        case = {
            "id": "persona-fixture",
            "suite": "persona_distillation",
            "mode": "persona_distillation_fixture",
            "persona_distillation": {
                "expected_items": [
                    {
                        "id": "decision",
                        "item_type": "decision_pattern",
                        "memory_layer": "procedural",
                        "content_keywords": ["escalate missing administrator review"],
                        "evidence": "privileged access review excludes administrators",
                    }
                ],
                "results": {
                    "deterministic": {
                        "items": [
                            {
                                "title": "Access review rule",
                                "content": "Review privileged access.",
                                "item_type": "domain_knowledge",
                                "memory_layer": "semantic",
                                "source_evidence": "privileged access review",
                                "evidence_verified": True,
                            }
                        ],
                        "metrics": {"latency_ms": 5, "cost_usd": 0.0},
                    },
                    "hybrid": {
                        "items": [
                            {
                                "title": "Escalate missing administrator review",
                                "content": "Escalate missing administrator review coverage.",
                                "item_type": "decision_pattern",
                                "memory_layer": "procedural",
                                "source_evidence": "privileged access review excludes administrators",
                                "evidence_verified": True,
                            }
                        ],
                        "metrics": {"latency_ms": 25, "cost_usd": 0.01},
                    },
                },
            },
            "assertions": [
                {"type": "persona_metric_min", "mode": "hybrid", "metric": "recall", "value": 1.0},
                {"type": "persona_metric_gte", "mode": "hybrid", "baseline": "deterministic", "metric": "f1"},
                {"type": "persona_best_mode", "mode": "hybrid", "metric": "f1"},
            ],
        }

        assertion_results = evaluate_persona_distillation_case(case)
        result = run_case(case)

        self.assertTrue(all(item.passed for item in assertion_results))
        self.assertTrue(result.passed)
        self.assertEqual(result.score, 100.0)
        metrics = assertion_results[0].actual
        self.assertEqual(metrics["hybrid"]["recall"], 1.0)
        self.assertGreater(metrics["hybrid"]["f1"], metrics["deterministic"]["f1"])

    def test_live_persona_distillation_requires_source_memories(self) -> None:
        case = {
            "id": "persona-live-missing-source",
            "suite": "persona_distillation",
            "mode": "persona_distillation_fixture",
            "persona_distillation": {
                "expected_items": [
                    {
                        "id": "decision",
                        "item_type": "decision_pattern",
                        "memory_layer": "procedural",
                        "content_keywords": ["escalate"],
                    }
                ],
                "results": {},
            },
        }

        result = run_case(
            case,
            base_url="http://backend.test",
            live_persona_distillation=True,
        )

        self.assertFalse(result.passed)
        self.assertIn("source_memories", result.error or "")

    def test_persona_best_mode_accepts_tied_winner(self) -> None:
        case = {
            "id": "persona-tied-best-mode",
            "suite": "persona_distillation",
            "mode": "persona_distillation_fixture",
            "persona_distillation": {
                "expected_items": [
                    {
                        "id": "decision",
                        "item_type": "decision_pattern",
                        "memory_layer": "procedural",
                        "content_keywords": ["escalate"],
                    }
                ],
                "results": {
                    "deterministic": {
                        "items": [
                            {
                                "title": "Escalate",
                                "content": "Escalate the issue.",
                                "item_type": "decision_pattern",
                                "memory_layer": "procedural",
                                "evidence_verified": True,
                            }
                        ],
                    },
                    "llm": {
                        "items": [
                            {
                                "title": "Escalate",
                                "content": "Escalate the issue.",
                                "item_type": "decision_pattern",
                                "memory_layer": "procedural",
                                "evidence_verified": True,
                            }
                        ],
                    },
                },
            },
            "assertions": [
                {"type": "persona_best_mode", "mode": "llm", "metric": "recall"},
            ],
        }

        assertion_results = evaluate_persona_distillation_case(case)

        self.assertTrue(all(item.passed for item in assertion_results))
        best_mode = assertion_results[-1]
        self.assertEqual(best_mode.actual, ["deterministic", "llm"])

    def test_live_persona_item_prefers_verified_evidence_from_merged_source_refs(self) -> None:
        item = {
            "title": "Merged workflow",
            "content": "Merged workflow content",
            "item_type": "workflow",
            "memory_layer": "procedural",
            "structured_payload": {
                "source_ref": {"memory_id": "memory-1"},
                "source_refs": [
                    {"memory_id": "memory-1"},
                    {
                        "memory_id": "memory-1",
                        "evidence": {
                            "text": "link the Jira ticket, attach the workpaper",
                            "verified": True,
                        },
                    },
                ],
            },
            "metadata": {},
        }

        projected = _live_persona_item(item)

        self.assertEqual(projected["source_evidence"], "link the Jira ticket, attach the workpaper")
        self.assertTrue(projected["evidence_verified"])

    def test_live_persona_distillation_replays_backend_and_scores_items(self) -> None:
        case = {
            "id": "persona-live",
            "suite": "persona_distillation",
            "mode": "persona_distillation_fixture",
            "persona_distillation": {
                "source_memories": [
                    {
                        "id": "source-a",
                        "content": "Admin accounts were not in the access review.",
                    }
                ],
                "expected_items": [
                    {
                        "id": "decision",
                        "item_type": "decision_pattern",
                        "memory_layer": "procedural",
                        "content_keywords": ["escalate missing administrator review"],
                        "evidence": "admin accounts were not in the access review",
                    }
                ],
                "results": {},
            },
            "assertions": [
                {"type": "persona_metric_min", "mode": "llm", "metric": "recall", "value": 1.0}
            ],
        }

        fake_client = _FakeLivePersonaEvalClient()
        with patch("scripts.run_evals.httpx.Client", return_value=fake_client):
            live_case = run_live_persona_distillation_case(
                case,
                base_url="http://backend.test",
                timeout_seconds=30.0,
                options=LivePersonaDistillationOptions(modes=("llm",), llm_model_source="main_agent"),
            )

        results = live_case["persona_distillation"]["results"]
        self.assertIn("llm", results)
        self.assertEqual(results["llm"]["metrics"]["llm_call_count"], 1)
        self.assertEqual(fake_client.memory_payloads[0]["memory"]["scope"], "user")
        self.assertEqual(fake_client.distill_payloads[0]["distillation_mode"], "llm")
        self.assertEqual(fake_client.distill_payloads[0]["llm_model_source"], "main_agent")
        assertion_results = evaluate_persona_distillation_case(live_case)
        self.assertTrue(all(item.passed for item in assertion_results))

    def test_run_case_live_persona_uses_live_result_mode(self) -> None:
        case = {
            "id": "persona-live-run-case",
            "suite": "persona_distillation",
            "mode": "persona_distillation_fixture",
            "persona_distillation": {
                "source_memories": [{"content": "Admin accounts were not in the access review."}],
                "expected_items": [
                    {
                        "id": "decision",
                        "item_type": "decision_pattern",
                        "memory_layer": "procedural",
                        "content_keywords": ["escalate missing administrator review"],
                        "evidence": "admin accounts were not in the access review",
                    }
                ],
                "results": {},
            },
            "assertions": [
                {"type": "persona_metric_min", "mode": "llm", "metric": "recall", "value": 1.0}
            ],
        }

        with patch("scripts.run_evals.httpx.Client", return_value=_FakeLivePersonaEvalClient()):
            result = run_case(
                case,
                base_url="http://backend.test",
                live_persona_distillation=True,
                live_persona_options=LivePersonaDistillationOptions(modes=("llm",)),
            )

        self.assertTrue(result.passed)
        self.assertEqual(result.mode, "persona_distillation_live")

    def test_persona_runtime_fixture_scores_published_persona_answers(self) -> None:
        case = {
            "id": "persona-runtime",
            "suite": "persona_runtime",
            "mode": "persona_runtime_fixture",
            "persona_runtime": {
                "fixture_type": "audit_runtime",
                "questions": [
                    {
                        "id": "q1",
                        "prompt": "What should happen when admin review coverage is missing?",
                        "expected_keywords": ["escalate", "administrator review"],
                        "style_keywords": ["concise"],
                        "source_keywords": ["privileged access review"],
                        "graph_context_keywords": ["access review escalation"],
                    },
                    {
                        "id": "q2",
                        "prompt": "Can you confirm the owner approved it?",
                        "expected_keywords": ["not enough evidence"],
                        "requires_uncertainty": True,
                        "uncertainty_keywords": ["not enough evidence"],
                    },
                ],
                "personas": {
                    "deterministic": {
                        "persona_version_id": "version-det",
                        "distillation_mode": "deterministic",
                        "answers": [
                            {
                                "question_id": "q1",
                                "answer": "Review administrator access.",
                                "citations": ["privileged access review"],
                            },
                            {"question_id": "q2", "answer": "Yes."},
                        ],
                    },
                    "hybrid": {
                        "persona_version_id": "version-hybrid",
                        "distillation_mode": "hybrid",
                        "answers": [
                            {
                                "question_id": "q1",
                                "answer": "Concise answer: escalate missing administrator review coverage.",
                                "citations": ["privileged access review"],
                                "graph_context": {"used_nodes": ["access review escalation"]},
                            },
                            {"question_id": "q2", "answer": "There is not enough evidence to confirm owner approval."},
                        ],
                    },
                },
            },
            "assertions": [
                {"type": "persona_runtime_metric_min", "mode": "hybrid", "metric": "overall", "value": 1.0},
                {"type": "persona_runtime_metric_gte", "mode": "hybrid", "baseline": "deterministic", "metric": "overall"},
                {"type": "persona_runtime_best_mode", "mode": "hybrid", "metric": "overall"},
            ],
        }

        assertion_results = evaluate_persona_runtime_case(case)
        result = run_case(case)

        self.assertTrue(all(item.passed for item in assertion_results))
        self.assertTrue(result.passed)
        report = assertion_results[0].actual
        self.assertEqual(report["fixture_type"], "audit_runtime")
        self.assertEqual(report["modes"]["hybrid"]["persona_version_id"], "version-hybrid")
        self.assertEqual(report["winners"]["overall"], "hybrid")

    def test_load_eval_cases_rejects_duplicate_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "one.yaml").write_text(yaml.safe_dump({"id": "duplicate", "mode": "fixture"}), encoding="utf-8")
            (root / "two.json").write_text(json.dumps({"id": "duplicate", "mode": "fixture"}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Duplicate eval case id"):
                load_eval_cases(root)

    def test_write_results_appends_jsonl(self) -> None:
        result = EvalResult(
            case_id="case-a",
            suite="unit",
            name="Case A",
            mode="fixture",
            passed=True,
            score=100.0,
            assertion_results=[
                AssertionResult(
                    assertion_type="status_completed",
                    passed=True,
                    message="execution status is completed",
                    expected="completed",
                    actual="completed",
                )
            ],
            execution_id="execution-a",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "nested" / "results.jsonl"

            write_results([result], path)
            write_results([copy.deepcopy(result)], path)

            lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        payload = json.loads(lines[0])
        self.assertEqual(payload["case_id"], "case-a")
        self.assertEqual(payload["assertions"][0]["type"], "status_completed")

    def test_summarize_results_counts_pass_fail_and_average_score(self) -> None:
        results = [
            EvalResult("case-pass", "unit", "Pass", "fixture", True, 100.0, []),
            EvalResult("case-fail", "unit", "Fail", "fixture", False, 50.0, []),
        ]

        summary = summarize_results(results)

        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["average_score"], 75.0)

class _FakeHTTPResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return copy.deepcopy(self.payload)


class _FakeLivePersonaEvalClient:
    def __init__(self) -> None:
        self.memory_payloads: list[dict] = []
        self.distill_payloads: list[dict] = []

    def __enter__(self) -> "_FakeLivePersonaEvalClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def post(self, path: str, json: dict) -> _FakeHTTPResponse:
        if path == "/users/sync":
            return _FakeHTTPResponse({"id": json["id"], "email": json["email"]})
        if path == "/memories":
            self.memory_payloads.append(copy.deepcopy(json))
            return _FakeHTTPResponse({"id": f"memory-{len(self.memory_payloads)}"})
        if path == "/persona-factory/distill":
            self.distill_payloads.append(copy.deepcopy(json))
            return _FakeHTTPResponse(
                {
                    "persona": {"id": "persona-live"},
                    "run": {
                        "id": "run-live",
                        "status": "needs_review",
                        "distillation_metrics": {
                            "llm_distillation": {
                                "call_count": 1,
                                "success_count": 1,
                                "failure_count": 0,
                                "total_latency_ms": 42,
                            }
                        },
                    },
                    "items": [
                        {
                            "title": "Escalate missing administrator review",
                            "content": "Escalate missing administrator review coverage.",
                            "item_type": "decision_pattern",
                            "memory_layer": "procedural",
                            "confidence": 0.9,
                            "needs_review": False,
                            "structured_payload": {
                                "source_ref": {
                                    "evidence_text": "admin accounts were not in the access review",
                                    "evidence_grounding": {"verified": True},
                                }
                            },
                            "metadata": {"review_flags": ["llm_only"]},
                        }
                    ],
                }
            )
        raise AssertionError(f"Unexpected POST path: {path}")


if __name__ == "__main__":
    unittest.main()
