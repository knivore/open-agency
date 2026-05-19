#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4


def _bootstrap_repo(script_file: str, *, reexec: bool) -> Path:
    script_path = Path(script_file).resolve()
    repo_root = script_path.parents[1]
    if reexec:
        venv_dir = repo_root / ".venv"
        venv_python = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        if venv_python.exists() and Path(sys.prefix).resolve() != venv_dir.resolve():
            os.execv(str(venv_python), [str(venv_python), str(script_path), *sys.argv[1:]])
    repo_root_text = str(repo_root)
    if repo_root_text not in sys.path:
        sys.path.insert(0, repo_root_text)
    return repo_root


REPO_ROOT = _bootstrap_repo(__file__, reexec=__name__ == "__main__")

import httpx
import yaml


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
DEFAULT_CASES_PATH = REPO_ROOT / "evals" / "cases"
DEFAULT_RESULTS_PATH = REPO_ROOT / ".data" / "evals" / "results.jsonl"
EVALUATION_TOOL_IDS = [
    "agency.execution.get",
    "agency.execution.events",
    "agency.execution.artifacts",
    "agency.workflow.get",
    "agency.workflow.list",
]


@dataclass(slots=True)
class EvidenceBundle:
    execution: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    workflow: dict[str, Any] | None = None

    @property
    def execution_id(self) -> str | None:
        value = self.execution.get("id")
        return str(value) if value else None

    @property
    def workflow_id(self) -> str | None:
        value = self.execution.get("workflow_id") or (self.workflow or {}).get("id")
        return str(value) if value else None


@dataclass(slots=True)
class AssertionResult:
    assertion_type: str
    passed: bool
    message: str
    expected: Any = None
    actual: Any = None

    def to_json(self) -> dict[str, Any]:
        return {
            "type": self.assertion_type,
            "passed": self.passed,
            "message": self.message,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass(slots=True)
class EvalResult:
    case_id: str
    suite: str
    name: str
    mode: str
    passed: bool
    score: float
    assertion_results: list[AssertionResult]
    execution_id: str | None = None
    workflow_id: str | None = None
    judge_result: dict[str, Any] | None = None
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "suite": self.suite,
            "name": self.name,
            "mode": self.mode,
            "passed": self.passed,
            "score": self.score,
            "execution_id": self.execution_id,
            "workflow_id": self.workflow_id,
            "assertions": [item.to_json() for item in self.assertion_results],
            "judge": self.judge_result,
            "error": self.error,
        }


def load_eval_cases(path: Path) -> list[dict[str, Any]]:
    paths = _case_paths(path)
    cases = [_load_case(item) for item in paths]
    seen: set[str] = set()
    for case in cases:
        case_id = str(case.get("id") or "").strip()
        if not case_id:
            raise ValueError(f"Eval case in {case.get('_path')} is missing id.")
        if case_id in seen:
            raise ValueError(f"Duplicate eval case id: {case_id}")
        seen.add(case_id)
    return cases


def _case_paths(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.exists():
        raise FileNotFoundError(f"Eval case path does not exist: {path}")
    suffixes = {".yaml", ".yml", ".json"}
    return sorted(item for item in path.rglob("*") if item.is_file() and item.suffix.lower() in suffixes)


def _load_case(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        if path.suffix.lower() == ".json":
            payload = json.load(handle)
        else:
            payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Eval case must be an object: {path}")
    payload["_path"] = str(path)
    return payload


def filter_cases(
        cases: list[dict[str, Any]],
        *,
        suite: str | None = None,
        case_id: str | None = None,
) -> list[dict[str, Any]]:
    filtered = cases
    if suite:
        filtered = [case for case in filtered if str(case.get("suite") or "default") == suite]
    if case_id:
        filtered = [case for case in filtered if case.get("id") == case_id]
    return filtered


def run_case(
        case: dict[str, Any],
        *,
        base_url: str | None = None,
        judge_agent: bool = False,
        timeout_seconds: float = 60.0,
) -> EvalResult:
    case_id = str(case["id"])
    suite = str(case.get("suite") or "default")
    name = str(case.get("name") or case_id)
    mode = str(case.get("mode") or "fixture")
    try:
        evidence = collect_evidence(case, base_url=base_url, timeout_seconds=timeout_seconds)
        assertion_results = evaluate_assertions(case.get("assertions") or [], evidence)
        deterministic_passed = all(item.passed for item in assertion_results)
        score = _score(assertion_results)
        judge_result = None
        if judge_agent or _case_uses_evaluation_agent(case):
            judge_result = run_evaluation_agent_judge(
                case,
                evidence,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
            )
        judge_passed = _judge_passed(judge_result)
        return EvalResult(
            case_id=case_id,
            suite=suite,
            name=name,
            mode=mode,
            passed=deterministic_passed and judge_passed,
            score=score,
            assertion_results=assertion_results,
            execution_id=evidence.execution_id,
            workflow_id=evidence.workflow_id,
            judge_result=judge_result,
        )
    except Exception as exc:
        return EvalResult(
            case_id=case_id,
            suite=suite,
            name=name,
            mode=mode,
            passed=False,
            score=0.0,
            assertion_results=[],
            error=str(exc),
        )


def collect_evidence(
        case: dict[str, Any],
        *,
        base_url: str | None,
        timeout_seconds: float,
) -> EvidenceBundle:
    mode = str(case.get("mode") or "fixture")
    if mode == "fixture":
        return _fixture_evidence(case)
    if mode == "existing_execution":
        if not base_url:
            raise ValueError("existing_execution evals require --base-url.")
        execution_id = _required_string(case, "execution_id")
        return _fetch_execution_evidence(base_url, execution_id)
    if mode == "http_workflow":
        if not base_url:
            raise ValueError("http_workflow evals require --base-url.")
        return _run_http_workflow_case(case, base_url=base_url, timeout_seconds=timeout_seconds)
    raise ValueError(f"Unsupported eval mode: {mode}")


def _fixture_evidence(case: dict[str, Any]) -> EvidenceBundle:
    evidence = case.get("evidence") if isinstance(case.get("evidence"), dict) else {}
    execution = dict(evidence.get("execution") or {})
    if "output_json" in execution and "output_payload" not in execution:
        execution["output_payload"] = execution["output_json"]
    return EvidenceBundle(
        execution=execution,
        events=list(evidence.get("events") or []),
        artifacts=list(evidence.get("artifacts") or []),
        workflow=evidence.get("workflow") if isinstance(evidence.get("workflow"), dict) else None,
    )


def _run_http_workflow_case(case: dict[str, Any], *, base_url: str, timeout_seconds: float) -> EvidenceBundle:
    workflow_id = _required_string(case, "workflow_id")
    payload = {
        "input": case.get("input") if isinstance(case.get("input"), dict) else {},
        "trigger": case.get("trigger") if isinstance(case.get("trigger"), dict) else {"created_by": "run_evals"},
    }
    if isinstance(case.get("runtime_adapter_id"), str):
        payload["runtimeAdapterId"] = case["runtime_adapter_id"]
    if isinstance(case.get("workflow_definition"), dict):
        payload["workflow_definition"] = case["workflow_definition"]
    if isinstance(case.get("model_profiles"), list):
        payload["model_profiles"] = case["model_profiles"]

    with httpx.Client(base_url=base_url.rstrip("/"), timeout=30.0) as client:
        response = client.post(f"/workflows/{workflow_id}/executions/start", json=payload)
        response.raise_for_status()
        execution_id = response.json()["execution"]["id"]
        final_payload = _poll_execution(client, execution_id, timeout_seconds=timeout_seconds)
    return _fetch_execution_evidence(base_url, execution_id, final_payload=final_payload)


def _fetch_execution_evidence(
        base_url: str,
        execution_id: str,
        *,
        final_payload: dict[str, Any] | None = None,
) -> EvidenceBundle:
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=30.0) as client:
        if final_payload is None:
            execution_response = client.get(f"/executions/{execution_id}")
            execution_response.raise_for_status()
            payload = execution_response.json()
        else:
            payload = final_payload
        execution = _extract_execution(payload)
        events_response = client.get(f"/executions/{execution_id}/events")
        events_response.raise_for_status()
        artifacts_response = client.get(f"/executions/{execution_id}/artifacts")
        artifacts_response.raise_for_status()
        workflow = None
        workflow_id = execution.get("workflow_id")
        if workflow_id:
            workflow_response = client.get(f"/workflows/{workflow_id}")
            if workflow_response.status_code == 200:
                workflow = _extract_workflow(workflow_response.json())
    return EvidenceBundle(
        execution=execution,
        events=events_response.json().get("items", []),
        artifacts=artifacts_response.json().get("items", []),
        workflow=workflow,
    )


def _poll_execution(client: httpx.Client, execution_id: str, *, timeout_seconds: float) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        response = client.get(f"/executions/{execution_id}")
        response.raise_for_status()
        payload = response.json()
        execution = _extract_execution(payload)
        if str(execution.get("status")) in TERMINAL_STATUSES:
            return payload
        time.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for execution '{execution_id}'")


def _extract_execution(payload: dict[str, Any]) -> dict[str, Any]:
    execution = payload.get("execution") if isinstance(payload.get("execution"), dict) else payload
    return dict(execution)


def _extract_workflow(payload: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(payload.get("workflow"), dict):
        return dict(payload["workflow"])
    if isinstance(payload.get("item"), dict):
        return dict(payload["item"])
    if isinstance(payload.get("id"), str):
        return dict(payload)
    return None


def evaluate_assertions(assertions: list[dict[str, Any]], evidence: EvidenceBundle) -> list[AssertionResult]:
    results: list[AssertionResult] = []
    for assertion in assertions:
        if not isinstance(assertion, dict):
            results.append(AssertionResult("invalid", False, "Assertion must be an object.", expected=assertion))
            continue
        assertion_type = str(assertion.get("type") or "")
        try:
            results.append(_evaluate_assertion(assertion_type, assertion, evidence))
        except Exception as exc:
            results.append(AssertionResult(assertion_type or "invalid", False, str(exc), expected=assertion))
    return results


def _evaluate_assertion(
        assertion_type: str,
        assertion: dict[str, Any],
        evidence: EvidenceBundle,
) -> AssertionResult:
    if assertion_type == "status_completed":
        actual = evidence.execution.get("status")
        return _result(assertion_type, actual == "completed", "execution status is completed", "completed", actual)
    if assertion_type == "status_equals":
        expected = assertion.get("value")
        actual = evidence.execution.get("status")
        return _result(assertion_type, actual == expected, "execution status matches", expected, actual)
    if assertion_type == "final_output_contains":
        expected = str(assertion.get("value") or "")
        actual = _final_output(evidence)
        return _result(assertion_type, expected in actual, "final output contains expected text", expected, actual)
    if assertion_type == "final_output_equals":
        expected = str(assertion.get("value") or "")
        actual = _final_output(evidence)
        return _result(assertion_type, actual == expected, "final output equals expected text", expected, actual)
    if assertion_type == "output_path_equals":
        path = _required_assertion_string(assertion, "path")
        expected = assertion.get("value")
        actual = _get_path(evidence.execution.get("output_payload"), path)
        return _result(assertion_type, actual == expected, f"output path {path} equals expected value", expected, actual)
    if assertion_type == "event_sequence_contains":
        expected = [str(item) for item in assertion.get("events") or assertion.get("value") or []]
        actual = [str(event.get("event_type")) for event in evidence.events]
        passed = _is_subsequence(expected, actual)
        return _result(assertion_type, passed, "event sequence contains expected ordered events", expected, actual)
    if assertion_type == "event_contains":
        matched = _matching_events(assertion, evidence.events)
        return _result(assertion_type, bool(matched), "matching event exists", assertion, matched[:3])
    if assertion_type == "tool_called":
        tool_id = str(assertion.get("tool_id") or assertion.get("tool_name") or assertion.get("value") or "")
        matched = [
            event for event in evidence.events
            if event.get("event_type") in {"tool.call.started", "tool.call.completed"}
            and _event_tool_matches(event, tool_id)
        ]
        return _result(assertion_type, bool(matched), "tool was called", tool_id, _event_summaries(matched))
    if assertion_type == "tool_not_called":
        tool_id = str(assertion.get("tool_id") or assertion.get("tool_name") or assertion.get("value") or "")
        matched = [
            event for event in evidence.events
            if event.get("event_type") in {"tool.call.started", "tool.call.completed"}
            and _event_tool_matches(event, tool_id)
        ]
        return _result(assertion_type, not matched, "tool was not called", tool_id, _event_summaries(matched))
    if assertion_type == "approval_requested":
        matched = [event for event in evidence.events if event.get("event_type") == "approval.requested"]
        return _result(assertion_type, bool(matched), "approval was requested", True, _event_summaries(matched))
    if assertion_type == "max_total_tokens":
        expected = int(assertion.get("value") or 0)
        actual = _total_tokens(evidence.events)
        return _result(assertion_type, actual <= expected, "total token usage is under threshold", expected, actual)
    if assertion_type == "artifact_contains":
        expected = str(assertion.get("value") or "")
        name = assertion.get("name")
        matched = []
        for artifact in evidence.artifacts:
            if isinstance(name, str) and artifact.get("name") != name:
                continue
            if expected in _artifact_text(artifact):
                matched.append(artifact)
        return _result(assertion_type, bool(matched), "artifact contains expected text", expected, [item.get("name") for item in matched])
    raise ValueError(f"Unsupported assertion type: {assertion_type}")


def _matching_events(assertion: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matched = events
    event_type = assertion.get("event_type")
    if isinstance(event_type, str):
        matched = [event for event in matched if event.get("event_type") == event_type]
    agent_id = assertion.get("agent_id")
    if isinstance(agent_id, str):
        matched = [event for event in matched if event.get("agent_id") == agent_id]
    task_id = assertion.get("task_id")
    if isinstance(task_id, str):
        matched = [event for event in matched if event.get("task_id") == task_id]
    tool_id = assertion.get("tool_id") or assertion.get("tool_name")
    if isinstance(tool_id, str):
        matched = [event for event in matched if _event_tool_matches(event, tool_id)]
    payload_path = assertion.get("payload_path")
    if isinstance(payload_path, str):
        expected = assertion.get("value")
        matched = [event for event in matched if _get_path(event.get("payload") or {}, payload_path) == expected]
    return matched


def _event_tool_matches(event: dict[str, Any], tool_id: str) -> bool:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    values = {
        str(payload.get("tool_id") or ""),
        str(payload.get("tool_name") or ""),
        str(event.get("tool_call_id") or ""),
    }
    return tool_id in values


def _event_summaries(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "event_type": event.get("event_type"),
            "sequence": event.get("sequence"),
            "tool_id": (event.get("payload") or {}).get("tool_id") if isinstance(event.get("payload"), dict) else None,
            "tool_name": (event.get("payload") or {}).get("tool_name") if isinstance(event.get("payload"), dict) else None,
        }
        for event in events[:5]
    ]


def _result(assertion_type: str, passed: bool, message: str, expected: Any, actual: Any) -> AssertionResult:
    return AssertionResult(
        assertion_type=assertion_type,
        passed=passed,
        message=message if passed else f"Failed: {message}",
        expected=expected,
        actual=actual,
    )


def _final_output(evidence: EvidenceBundle) -> str:
    output = evidence.execution.get("output_payload")
    if output is None:
        output = evidence.execution.get("output_json")
    final = _get_path(output, "final_output") if isinstance(output, dict) else output
    if final is None and isinstance(output, dict):
        final = _get_path(output, "node_outputs")
    if isinstance(final, str):
        return final
    return json.dumps(final, sort_keys=True, default=str) if final is not None else ""


def _total_tokens(events: list[dict[str, Any]]) -> int:
    total = 0
    for event in events:
        metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
        usage = (event.get("payload") or {}).get("usage") if isinstance(event.get("payload"), dict) else {}
        if isinstance(metrics.get("total_tokens"), int):
            total += int(metrics["total_tokens"])
        elif isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int):
            total += int(usage["total_tokens"])
    return total


def _artifact_text(artifact: dict[str, Any]) -> str:
    parts: list[str] = []
    content_text = artifact.get("content_text")
    if isinstance(content_text, str):
        parts.append(content_text)
    content_json = artifact.get("content_json")
    if content_json is not None:
        parts.append(json.dumps(content_json, sort_keys=True, default=str))
    uri = artifact.get("uri") or artifact.get("file_path")
    if isinstance(uri, str):
        parts.append(uri)
    return "\n".join(parts)


def _get_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if part == "":
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        else:
            return None
    return current


def _is_subsequence(expected: list[str], actual: list[str]) -> bool:
    if not expected:
        return True
    position = 0
    for item in actual:
        if item == expected[position]:
            position += 1
            if position == len(expected):
                return True
    return False


def run_evaluation_agent_judge(
        case: dict[str, Any],
        evidence: EvidenceBundle,
        *,
        base_url: str | None,
        timeout_seconds: float,
) -> dict[str, Any]:
    if not base_url:
        return {
            "status": "skipped",
            "reason": "evaluation_agent judge requires --base-url and a configured backend.",
        }
    if not evidence.execution_id:
        return {"status": "skipped", "reason": "evaluation_agent judge requires a persisted execution_id."}

    with httpx.Client(base_url=base_url.rstrip("/"), timeout=30.0) as client:
        agent = _find_evaluation_agent(client)
        tool_definitions = [_get_api_item(client, f"/tools/{tool_id}") for tool_id in EVALUATION_TOOL_IDS]
        workflow_id = f"eval-judge-{_slug(case['id'])}-{uuid4().hex[:8]}"
        task_id = f"{workflow_id}-task"
        node_id = f"{workflow_id}-node"
        judge_input = _judge_input(case, evidence)
        agent_payload = dict(agent)
        agent_payload["tool_ids"] = [tool["id"] for tool in tool_definitions]
        workflow_definition = {
            "id": workflow_id,
            "name": f"Evaluation Judge - {case['id']}",
            "description": "Ephemeral workflow for evaluation-agent judging.",
            "entrypoint": node_id,
            "nodes": [
                {
                    "id": node_id,
                    "name": "Judge",
                    "node_type": "task",
                    "task_id": task_id,
                    "agent_id": agent_payload["id"],
                }
            ],
            "task_definitions": [
                {
                    "id": task_id,
                    "name": "Judge Run",
                    "description": (
                        "Evaluate the target execution using the supplied rubric. Inspect the target run with "
                        "read-only tools if needed. Return only the JSON verdict required by your instructions."
                    ),
                    "expected_output": "A JSON evaluation verdict.",
                    "agent_id": agent_payload["id"],
                    "tool_ids": [tool["id"] for tool in tool_definitions],
                }
            ],
            "agent_definitions": [agent_payload],
            "tool_definitions": tool_definitions,
            "default_runtime_adapter_id": "native",
            "metadata": {"source": "run_evals", "target_execution_id": evidence.execution_id},
        }
        response = client.post(
            f"/workflows/{workflow_id}/executions/start",
            json={
                "input": judge_input,
                "trigger": {"created_by": "run_evals", "type": "evaluation_agent_judge"},
                "workflow_definition": workflow_definition,
            },
        )
        response.raise_for_status()
        judge_execution_id = response.json()["execution"]["id"]
        final_payload = _poll_execution(client, judge_execution_id, timeout_seconds=timeout_seconds)
    judge_execution = _extract_execution(final_payload)
    return {
        "status": "completed" if judge_execution.get("status") == "completed" else "error",
        "judge_execution_id": judge_execution_id,
        "execution_status": judge_execution.get("status"),
        "verdict": _parse_judge_verdict(judge_execution.get("output_payload")),
        "raw_output": judge_execution.get("output_payload"),
        "error": judge_execution.get("error"),
    }


def _find_evaluation_agent(client: httpx.Client) -> dict[str, Any]:
    payload = _get_api_item(client, "/agents", expect_collection=True)
    agents = payload if isinstance(payload, list) else payload.get("items", [])
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        metadata = agent.get("metadata") if isinstance(agent.get("metadata"), dict) else {}
        hints = agent.get("framework_hints") if isinstance(agent.get("framework_hints"), dict) else {}
        hint_metadata = hints.get("metadata") if isinstance(hints.get("metadata"), dict) else {}
        if (
                str(agent.get("name", "")).strip().lower() == "evaluation"
                or metadata.get("agent_kind") == "evaluation"
                or hint_metadata.get("agent_kind") == "evaluation"
        ):
            return agent
    raise RuntimeError("Evaluation agent was not found. Run make setup-evaluation-agent first.")


def _get_api_item(client: httpx.Client, path: str, *, expect_collection: bool = False) -> Any:
    response = client.get(path)
    response.raise_for_status()
    payload = response.json()
    if expect_collection:
        return payload
    if isinstance(payload, dict):
        if "item" in payload:
            return payload["item"]
        if "tool" in payload:
            return payload["tool"]
    return payload


def _judge_input(case: dict[str, Any], evidence: EvidenceBundle) -> dict[str, Any]:
    return {
        "eval_case": {
            "id": case.get("id"),
            "name": case.get("name"),
            "suite": case.get("suite"),
            "rubric": case.get("rubric"),
            "expected_behavior": case.get("expected_behavior"),
            "assertions": case.get("assertions", []),
        },
        "target_execution_id": evidence.execution_id,
        "target_workflow_id": evidence.workflow_id,
        "provided_evidence_summary": {
            "execution_status": evidence.execution.get("status"),
            "final_output": _final_output(evidence)[:2000],
            "event_count": len(evidence.events),
            "artifact_count": len(evidence.artifacts),
        },
    }


def _parse_judge_verdict(output_payload: Any) -> dict[str, Any] | None:
    final_output = _get_path(output_payload, "final_output") if isinstance(output_payload, dict) else output_payload
    if isinstance(final_output, dict):
        return final_output
    if isinstance(final_output, str):
        stripped = final_output.strip()
        if stripped.startswith("```"):
            stripped = stripped.strip("`")
            if stripped.startswith("json"):
                stripped = stripped[4:].strip()
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def write_results(results: list[EvalResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result.to_json(), sort_keys=True, default=str) + "\n")


def summarize_results(results: list[EvalResult]) -> dict[str, Any]:
    passed = [result for result in results if result.passed]
    return {
        "total": len(results),
        "passed": len(passed),
        "failed": len(results) - len(passed),
        "average_score": round(sum(result.score for result in results) / max(len(results), 1), 2),
        "results": [result.to_json() for result in results],
    }


def _score(assertion_results: list[AssertionResult]) -> float:
    if not assertion_results:
        return 100.0
    return round(100.0 * sum(1 for item in assertion_results if item.passed) / len(assertion_results), 2)


def _case_uses_evaluation_agent(case: dict[str, Any]) -> bool:
    judge = case.get("judge")
    return isinstance(judge, dict) and judge.get("type") == "evaluation_agent"


def _judge_passed(judge_result: dict[str, Any] | None) -> bool:
    if judge_result is None:
        return True
    if judge_result.get("status") == "skipped":
        return True
    if judge_result.get("status") != "completed":
        return False
    verdict = judge_result.get("verdict")
    return not isinstance(verdict, dict) or verdict.get("passed") is not False


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required.")
    return value


def _required_assertion_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Assertion field {key} is required.")
    return value


def _slug(value: Any) -> str:
    return "".join(char if char.isalnum() else "-" for char in str(value).lower()).strip("-")[:40] or "case"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Agency evaluation cases.")
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH), help="Eval case file or directory.")
    parser.add_argument("--suite", help="Optional suite filter.")
    parser.add_argument("--case-id", help="Optional case id filter.")
    parser.add_argument("--base-url", help="Running Agency backend base URL for HTTP modes.")
    parser.add_argument("--timeout-seconds", type=float, default=60.0, help="Execution polling timeout.")
    parser.add_argument("--results-path", default=str(DEFAULT_RESULTS_PATH), help="JSONL results output path.")
    parser.add_argument("--fail-under", type=float, default=100.0, help="Minimum average score required.")
    parser.add_argument("--judge-agent", action="store_true", help="Invoke the Evaluation agent as a semantic judge.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON summary.")
    parser.add_argument("--no-write", action="store_true", help="Do not append results to JSONL.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cases = filter_cases(
        load_eval_cases(Path(args.cases)),
        suite=args.suite,
        case_id=args.case_id,
    )
    if not cases:
        print("No eval cases matched the selected filters.", file=sys.stderr)
        return 1
    results = [
        run_case(
            case,
            base_url=args.base_url,
            judge_agent=args.judge_agent,
            timeout_seconds=args.timeout_seconds,
        )
        for case in cases
    ]
    if not args.no_write:
        write_results(results, Path(args.results_path))
    summary = summarize_results(results)
    failed_threshold = summary["average_score"] < args.fail_under
    has_failures = summary["failed"] > 0
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    else:
        print(
            f"Eval results: {summary['passed']}/{summary['total']} passed, "
            f"average score {summary['average_score']}"
        )
        for result in results:
            marker = "PASS" if result.passed else "FAIL"
            detail = f" ({result.error})" if result.error else ""
            print(f"{marker} {result.case_id}: {result.score}{detail}")
    return 1 if has_failures or failed_threshold else 0


if __name__ == "__main__":
    raise SystemExit(main())
