from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.run_evals import (
    DEFAULT_CASES_PATH,
    EvalResult,
    AssertionResult,
    filter_cases,
    load_eval_cases,
    run_case,
    summarize_results,
    write_results,
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
        self.assertTrue(all(case.get("mode") == "fixture" for case in cases))
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


if __name__ == "__main__":
    unittest.main()
