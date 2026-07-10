#!/usr/bin/env python3
"""Run deterministic and optional live-backend evaluation cases.

Cases are YAML/JSON files under `evals/cases`. Offline mode evaluates declared
assertions against fixture evidence; live mode can create or inspect backend
executions and optionally ask the Evaluation agent to judge outcomes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

try:
    from scripts._bootstrap import bootstrap_repo
except ModuleNotFoundError:
    from _bootstrap import bootstrap_repo

REPO_ROOT = bootstrap_repo(__file__, reexec=__name__ == "__main__")

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


@dataclass(slots=True)
class LivePersonaDistillationOptions:
    modes: tuple[str, ...] = ("deterministic", "llm", "hybrid")
    user_id: str = "persona-eval-user"
    user_email: str = "persona-eval-user@example.local"
    llm_model_source: str | None = None
    model_profile_id: str | None = None
    llm_model_provider: str | None = None
    llm_model: str | None = None


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
        live_persona_distillation: bool = False,
        live_persona_options: LivePersonaDistillationOptions | None = None,
) -> EvalResult:
    case_id = str(case["id"])
    suite = str(case.get("suite") or "default")
    name = str(case.get("name") or case_id)
    mode = str(case.get("mode") or "fixture")
    try:
        if mode == "persona_distillation_fixture":
            if live_persona_distillation:
                if not base_url:
                    raise ValueError("live persona distillation evals require --base-url.")
                live_case = run_live_persona_distillation_case(
                    case,
                    base_url=base_url,
                    timeout_seconds=timeout_seconds,
                    options=live_persona_options or LivePersonaDistillationOptions(),
                )
                assertion_results = evaluate_persona_distillation_case(live_case)
                return EvalResult(
                    case_id=case_id,
                    suite=suite,
                    name=name,
                    mode="persona_distillation_live",
                    passed=all(item.passed for item in assertion_results),
                    score=_score(assertion_results),
                    assertion_results=assertion_results,
                )
            assertion_results = evaluate_persona_distillation_case(case)
            return EvalResult(
                case_id=case_id,
                suite=suite,
                name=name,
                mode=mode,
                passed=all(item.passed for item in assertion_results),
                score=_score(assertion_results),
                assertion_results=assertion_results,
            )
        if mode == "persona_runtime_fixture":
            assertion_results = evaluate_persona_runtime_case(case)
            return EvalResult(
                case_id=case_id,
                suite=suite,
                name=name,
                mode=mode,
                passed=all(item.passed for item in assertion_results),
                score=_score(assertion_results),
                assertion_results=assertion_results,
            )
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
    if mode in {"persona_distillation_fixture", "persona_runtime_fixture"}:
        return EvidenceBundle()
    raise ValueError(f"Unsupported eval mode: {mode}")


def run_live_persona_distillation_case(
        case: dict[str, Any],
        *,
        base_url: str,
        timeout_seconds: float,
        options: LivePersonaDistillationOptions,
) -> dict[str, Any]:
    persona_eval = case.get("persona_distillation") if isinstance(case.get("persona_distillation"), dict) else {}
    source_memories = _live_persona_source_memories(case)
    headers = {
        "x-agency-user-id": options.user_id,
        "x-agency-user-email": options.user_email,
    }
    timeout = httpx.Timeout(timeout_seconds)
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, headers=headers) as client:
        _sync_live_eval_user(client, options)
        memory_ids = [
            _create_live_eval_memory(client, case=case, source=source, options=options)
            for source in source_memories
        ]
        mode_results = {
            mode: _run_live_persona_distillation_mode(
                client,
                case=case,
                mode=mode,
                memory_ids=memory_ids,
                options=options,
            )
            for mode in options.modes
        }

    live_case = dict(case)
    live_persona_eval = dict(persona_eval)
    live_persona_eval["results"] = mode_results
    live_persona_eval["live_run"] = {
        "base_url": base_url.rstrip("/"),
        "modes": list(options.modes),
        "user_id": options.user_id,
        "source_memory_count": len(memory_ids),
    }
    live_case["persona_distillation"] = live_persona_eval
    live_case["assertions"] = _filter_persona_assertions_for_modes(case.get("assertions"), set(mode_results))
    return live_case


def _live_persona_source_memories(case: dict[str, Any]) -> list[dict[str, Any]]:
    persona_eval = case.get("persona_distillation") if isinstance(case.get("persona_distillation"), dict) else {}
    sources = persona_eval.get("source_memories")
    if isinstance(sources, list):
        normalized = [source for source in sources if isinstance(source, dict)]
    else:
        normalized = []
    if not normalized:
        raise ValueError(
            "persona_distillation.source_memories is required for --live-persona-distillation."
        )
    for index, source in enumerate(normalized):
        content = source.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"persona_distillation.source_memories[{index}].content is required.")
    return normalized


def _sync_live_eval_user(client: httpx.Client, options: LivePersonaDistillationOptions) -> None:
    response = client.post(
        "/users/sync",
        json={"id": options.user_id, "email": options.user_email},
    )
    response.raise_for_status()


def _create_live_eval_memory(
        client: httpx.Client,
        *,
        case: dict[str, Any],
        source: dict[str, Any],
        options: LivePersonaDistillationOptions,
) -> str:
    case_id = str(case.get("id") or "persona-live-eval")
    source_id = str(source.get("id") or f"{case_id}-source")
    payload = {
        "memory": {
            "scope": source.get("scope") or "user",
            "created_by_user_id": options.user_id,
            "content": source["content"],
            "summary": source.get("summary") or source.get("title") or case.get("name"),
            "tags": list(source.get("tags") or ["persona-live-eval", case_id]),
            "source": source.get("source") or "persona_live_eval",
            "memory_type": source.get("memory_type") or "archive",
            "metadata": {
                "eval_case_id": case_id,
                "eval_source_id": source_id,
                "eval_suite": case.get("suite"),
                **(source.get("metadata") if isinstance(source.get("metadata"), dict) else {}),
            },
        },
        "confirmed": True,
    }
    response = client.post("/memories", json=payload)
    response.raise_for_status()
    memory = response.json()
    memory_id = memory.get("id")
    if not isinstance(memory_id, str) or not memory_id:
        raise ValueError(f"Memory create response for case '{case_id}' did not include id.")
    return memory_id


def _run_live_persona_distillation_mode(
        client: httpx.Client,
        *,
        case: dict[str, Any],
        mode: str,
        memory_ids: list[str],
        options: LivePersonaDistillationOptions,
) -> dict[str, Any]:
    normalized_mode = mode.strip().lower()
    if normalized_mode not in {"deterministic", "llm", "hybrid"}:
        raise ValueError(f"Unsupported live persona distillation mode: {mode}")
    case_id = str(case.get("id") or "persona-live-eval")
    payload: dict[str, Any] = {
        "name": f"Eval {case_id} {normalized_mode} {uuid4().hex[:8]}",
        "description": f"Live Persona Factory eval for {case_id} in {normalized_mode} mode.",
        "source_memory_ids": memory_ids,
        "distillation_mode": normalized_mode,
        "persona_type": "professional",
        "capability_mode": "persona_plus_expertise",
        "consent_status": "explicit_consent",
        "source_basis": "memory_records",
        "sensitivity_level": "standard",
        "visibility": "private",
    }
    if normalized_mode in {"llm", "hybrid"}:
        if options.llm_model_source:
            payload["llm_model_source"] = options.llm_model_source
        if options.model_profile_id:
            payload["model_profile_id"] = options.model_profile_id
        if options.llm_model_provider:
            payload["llm_model_provider"] = options.llm_model_provider
        if options.llm_model:
            payload["llm_model"] = options.llm_model

    started = time.perf_counter()
    response = client.post("/persona-factory/distill", json=payload)
    latency_ms = int((time.perf_counter() - started) * 1000)
    if response.status_code >= 400:
        return {
            "items": [],
            "metrics": {"latency_ms": latency_ms, "cost_usd": 0.0},
            "status": "http_error",
            "error": _response_error_detail(response),
        }
    body = response.json()
    run = body.get("run") if isinstance(body.get("run"), dict) else {}
    metrics = _live_persona_metrics(run, latency_ms=latency_ms)
    return {
        "items": [
            _live_persona_item(item)
            for item in body.get("items") or []
            if isinstance(item, dict)
        ],
        "metrics": metrics,
        "run_id": run.get("id"),
        "persona_id": (body.get("persona") or {}).get("id") if isinstance(body.get("persona"), dict) else None,
        "status": run.get("status"),
    }


def _live_persona_item(item: dict[str, Any]) -> dict[str, Any]:
    structured_payload = item.get("structured_payload") if isinstance(item.get("structured_payload"), dict) else {}
    source_ref = structured_payload.get("source_ref") if isinstance(structured_payload.get("source_ref"), dict) else {}
    source_refs = structured_payload.get("source_refs") if isinstance(structured_payload.get("source_refs"), list) else []
    evidence_ref = _live_persona_evidence_ref(source_ref, source_refs)
    evidence_grounding = evidence_ref.get("evidence_grounding") if isinstance(evidence_ref.get("evidence_grounding"), dict) else {}
    if not evidence_grounding:
        evidence_grounding = evidence_ref.get("evidence") if isinstance(evidence_ref.get("evidence"), dict) else {}
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return {
        "title": item.get("title"),
        "content": item.get("content"),
        "item_type": item.get("item_type"),
        "memory_layer": item.get("memory_layer"),
        "confidence": item.get("confidence"),
        "needs_review": item.get("needs_review"),
        "source_evidence": evidence_ref.get("evidence_text")
        or _live_persona_evidence_text(evidence_ref)
        or structured_payload.get("source_evidence")
        or metadata.get("source_evidence")
        or item.get("content"),
        "evidence_verified": evidence_grounding.get("verified"),
        "structured_payload": structured_payload,
        "metadata": metadata,
        "review_flags": metadata.get("review_flags"),
        "conflict_group_id": metadata.get("conflict_group_id"),
    }


def _live_persona_evidence_ref(source_ref: dict[str, Any], source_refs: list[Any]) -> dict[str, Any]:
    refs = [ref for ref in [source_ref, *source_refs] if isinstance(ref, dict)]
    for ref in refs:
        evidence = ref.get("evidence") if isinstance(ref.get("evidence"), dict) else {}
        if evidence.get("verified") is True:
            return ref
    for ref in refs:
        if isinstance(ref.get("evidence"), dict) or isinstance(ref.get("evidence_grounding"), dict):
            return ref
    return source_ref


def _live_persona_evidence_text(source_ref: dict[str, Any]) -> Any:
    evidence = source_ref.get("evidence")
    if isinstance(evidence, dict):
        return evidence.get("text")
    return evidence


def _response_error_detail(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        payload = response.text
    return {
        "status_code": response.status_code,
        "body": payload,
    }


def _filter_persona_assertions_for_modes(assertions: Any, modes: set[str]) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for assertion in assertions or []:
        if not isinstance(assertion, dict):
            continue
        mode = assertion.get("mode")
        baseline = assertion.get("baseline")
        if isinstance(mode, str) and mode not in modes:
            continue
        if isinstance(baseline, str) and baseline not in modes:
            continue
        filtered.append(assertion)
    return filtered


def _live_persona_metrics(run: dict[str, Any], *, latency_ms: int) -> dict[str, Any]:
    distillation_metrics = run.get("distillation_metrics") if isinstance(run.get("distillation_metrics"), dict) else {}
    llm_metrics = distillation_metrics.get("llm_distillation") if isinstance(distillation_metrics.get("llm_distillation"), dict) else {}
    cost = (
        llm_metrics.get("cost_usd")
        or llm_metrics.get("estimated_cost_usd")
        or distillation_metrics.get("cost_usd")
        or 0.0
    )
    return {
        "latency_ms": latency_ms,
        "cost_usd": float(cost or 0.0),
        "llm_call_count": int(llm_metrics.get("call_count") or 0),
        "llm_success_count": int(llm_metrics.get("success_count") or 0),
        "llm_failure_count": int(llm_metrics.get("failure_count") or 0),
        "llm_total_latency_ms": int(llm_metrics.get("total_latency_ms") or 0),
    }


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


def evaluate_persona_distillation_case(case: dict[str, Any]) -> list[AssertionResult]:
    persona_eval = case.get("persona_distillation") if isinstance(case.get("persona_distillation"), dict) else {}
    expected_items = [
        item for item in persona_eval.get("expected_items") or []
        if isinstance(item, dict)
    ]
    mode_results = {
        str(mode): payload
        for mode, payload in (persona_eval.get("results") or {}).items()
        if isinstance(payload, dict)
    }
    if not expected_items:
        return [AssertionResult("persona_fixture_valid", False, "persona_distillation.expected_items is required.")]
    if not mode_results:
        return [AssertionResult("persona_fixture_valid", False, "persona_distillation.results is required.")]

    report = {
        mode: _score_persona_distillation_mode(expected_items, payload)
        for mode, payload in mode_results.items()
    }
    assertions = case.get("assertions") or _default_persona_assertions(report)
    results: list[AssertionResult] = [
        AssertionResult(
            "persona_metrics",
            True,
            "persona distillation metrics computed",
            expected={"modes": sorted(mode_results)},
            actual=report,
        )
    ]
    for assertion in assertions:
        if not isinstance(assertion, dict):
            results.append(AssertionResult("invalid", False, "Assertion must be an object.", expected=assertion))
            continue
        assertion_type = str(assertion.get("type") or "")
        try:
            results.append(_evaluate_persona_assertion(assertion_type, assertion, report))
        except Exception as exc:
            results.append(AssertionResult(assertion_type or "invalid", False, str(exc), expected=assertion))
    return results


def _score_persona_distillation_mode(
        expected_items: list[dict[str, Any]],
        result: dict[str, Any],
) -> dict[str, Any]:
    items = [item for item in result.get("items") or [] if isinstance(item, dict)]
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    matches = _match_persona_items(expected_items, items)
    matched_expected = {match["expected_index"] for match in matches}
    matched_actual = {match["actual_index"] for match in matches}
    precision = len(matched_actual) / len(items) if items else (1.0 if not expected_items else 0.0)
    recall = len(matched_expected) / len(expected_items) if expected_items else 1.0
    f1 = 0.0 if precision + recall == 0 else (2 * precision * recall) / (precision + recall)
    evidence_grounding = _evidence_grounding_score(matches, expected_items, items)
    item_type_accuracy = _field_accuracy(matches, expected_items, items, "item_type")
    memory_layer_accuracy = _field_accuracy(matches, expected_items, items, "memory_layer")
    conflict_quality = _conflict_detection_quality(expected_items, items)
    review_burden = sum(1 for item in items if item.get("needs_review") is True) / len(items) if items else 0.0
    return {
        "item_count": len(items),
        "matched_expected_count": len(matched_expected),
        "matched_item_count": len(matched_actual),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "evidence_grounding": round(evidence_grounding, 4),
        "item_type_accuracy": round(item_type_accuracy, 4),
        "memory_layer_accuracy": round(memory_layer_accuracy, 4),
        "conflict_detection_quality": round(conflict_quality, 4),
        "review_burden": round(review_burden, 4),
        "cost_usd": float(metrics.get("cost_usd") or 0.0),
        "latency_ms": int(metrics.get("latency_ms") or 0),
        "status": result.get("status"),
        "run_id": result.get("run_id"),
        "error": result.get("error"),
        "matches": matches,
    }


def _match_persona_items(expected_items: list[dict[str, Any]], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    used_actual: set[int] = set()
    for expected_index, expected in enumerate(expected_items):
        best: tuple[float, int] | None = None
        for actual_index, actual in enumerate(items):
            if actual_index in used_actual:
                continue
            score = _persona_item_match_score(expected, actual)
            if score <= 0:
                continue
            if best is None or score > best[0]:
                best = (score, actual_index)
        if best is None:
            continue
        used_actual.add(best[1])
        matches.append(
            {
                "expected_index": expected_index,
                "actual_index": best[1],
                "score": round(best[0], 4),
                "expected_id": expected.get("id"),
                "actual_title": items[best[1]].get("title"),
            }
        )
    return matches


def _persona_item_match_score(expected: dict[str, Any], actual: dict[str, Any]) -> float:
    if expected.get("item_type") and actual.get("item_type") and expected.get("item_type") != actual.get("item_type"):
        return 0.0
    if (
            expected.get("memory_layer")
            and actual.get("memory_layer")
            and expected.get("memory_layer") != actual.get("memory_layer")
    ):
        return 0.0
    expected_keywords = _normalized_keywords(expected.get("content_keywords") or expected.get("keywords") or [])
    if not expected_keywords:
        expected_keywords = _normalized_keywords([expected.get("content") or expected.get("title") or ""])
    haystack = _normalized_text(" ".join([
        str(actual.get("title") or ""),
        str(actual.get("content") or ""),
        json.dumps(actual.get("structured_payload") or {}, sort_keys=True, default=str),
    ]))
    keyword_hits = sum(1 for keyword in expected_keywords if keyword and keyword in haystack)
    keyword_score = keyword_hits / len(expected_keywords) if expected_keywords else 0.0
    type_bonus = 0.15 if expected.get("item_type") == actual.get("item_type") else 0.0
    layer_bonus = 0.1 if expected.get("memory_layer") == actual.get("memory_layer") else 0.0
    score = min(keyword_score + type_bonus + layer_bonus, 1.0)
    return score if score >= 0.45 else 0.0


def _normalized_keywords(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    return [_normalized_text(str(value)) for value in values if str(value).strip()]


def _normalized_text(value: str) -> str:
    return " ".join("".join(char.lower() if char.isalnum() else " " for char in value).split())


def _evidence_grounding_score(
        matches: list[dict[str, Any]],
        expected_items: list[dict[str, Any]],
        items: list[dict[str, Any]],
) -> float:
    if not matches:
        return 0.0
    grounded = 0
    for match in matches:
        expected = expected_items[match["expected_index"]]
        actual = items[match["actual_index"]]
        evidence_text = str(actual.get("source_evidence") or actual.get("evidence") or "")
        structured_payload = actual.get("structured_payload") if isinstance(actual.get("structured_payload"), dict) else {}
        evidence = structured_payload.get("source_evidence") or evidence_text
        expected_evidence = str(expected.get("evidence") or "")
        evidence_verified = actual.get("evidence_verified")
        if evidence_verified is False:
            continue
        if expected_evidence and _normalized_text(expected_evidence) not in _normalized_text(str(evidence)):
            continue
        if str(evidence).strip():
            grounded += 1
    return grounded / len(matches)


def _field_accuracy(
        matches: list[dict[str, Any]],
        expected_items: list[dict[str, Any]],
        items: list[dict[str, Any]],
        field: str,
) -> float:
    if not matches:
        return 0.0
    correct = sum(
        1
        for match in matches
        if expected_items[match["expected_index"]].get(field) == items[match["actual_index"]].get(field)
    )
    return correct / len(matches)


def _conflict_detection_quality(expected_items: list[dict[str, Any]], items: list[dict[str, Any]]) -> float:
    expected_conflict_count = sum(1 for item in expected_items if item.get("conflict_expected") is True)
    if expected_conflict_count == 0:
        return 1.0
    detected = 0
    for item in items:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        flags = item.get("review_flags") or metadata.get("review_flags") or []
        if not isinstance(flags, list):
            flags = []
        if item.get("conflict_group_id") or "material_conflict" in flags:
            detected += 1
    return min(detected / expected_conflict_count, 1.0)


def _default_persona_assertions(report: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    assertions: list[dict[str, Any]] = []
    for mode in sorted(report):
        assertions.extend(
            [
                {"type": "persona_metric_min", "mode": mode, "metric": "precision", "value": 0.7},
                {"type": "persona_metric_min", "mode": mode, "metric": "recall", "value": 0.7},
                {"type": "persona_metric_min", "mode": mode, "metric": "evidence_grounding", "value": 0.7},
            ]
        )
    if "hybrid" in report and "deterministic" in report:
        assertions.append({"type": "persona_metric_gte", "mode": "hybrid", "baseline": "deterministic", "metric": "f1"})
    return assertions


def _evaluate_persona_assertion(
        assertion_type: str,
        assertion: dict[str, Any],
        report: dict[str, dict[str, Any]],
) -> AssertionResult:
    if assertion_type == "persona_metric_min":
        mode = _required_assertion_string(assertion, "mode")
        metric = _required_assertion_string(assertion, "metric")
        expected = float(assertion.get("value") or 0.0)
        actual = float(report.get(mode, {}).get(metric, 0.0))
        return _result(assertion_type, actual >= expected, f"{mode}.{metric} meets minimum", expected, actual)
    if assertion_type == "persona_metric_max":
        mode = _required_assertion_string(assertion, "mode")
        metric = _required_assertion_string(assertion, "metric")
        expected = float(assertion.get("value") or 0.0)
        actual = float(report.get(mode, {}).get(metric, 0.0))
        return _result(assertion_type, actual <= expected, f"{mode}.{metric} stays under maximum", expected, actual)
    if assertion_type == "persona_metric_gte":
        mode = _required_assertion_string(assertion, "mode")
        baseline = _required_assertion_string(assertion, "baseline")
        metric = _required_assertion_string(assertion, "metric")
        actual = float(report.get(mode, {}).get(metric, 0.0))
        expected = float(report.get(baseline, {}).get(metric, 0.0))
        return _result(assertion_type, actual >= expected, f"{mode}.{metric} is at least {baseline}.{metric}", expected, actual)
    if assertion_type == "persona_best_mode":
        mode = _required_assertion_string(assertion, "mode")
        metric = _required_assertion_string(assertion, "metric")
        best_value = max(float(metrics.get(metric, 0.0)) for metrics in report.values()) if report else 0.0
        winners = [
            candidate_mode
            for candidate_mode, metrics in report.items()
            if float(metrics.get(metric, 0.0)) == best_value
        ]
        return _result(assertion_type, mode in winners, f"{mode} is best for {metric}", mode, winners)
    raise ValueError(f"Unsupported persona assertion type: {assertion_type}")


def evaluate_persona_runtime_case(case: dict[str, Any]) -> list[AssertionResult]:
    runtime_eval = case.get("persona_runtime") if isinstance(case.get("persona_runtime"), dict) else {}
    questions = [
        question for question in runtime_eval.get("questions") or []
        if isinstance(question, dict)
    ]
    personas = {
        str(mode): payload
        for mode, payload in (runtime_eval.get("personas") or {}).items()
        if isinstance(payload, dict)
    }
    if not questions:
        return [AssertionResult("persona_runtime_fixture_valid", False, "persona_runtime.questions is required.")]
    if not personas:
        return [AssertionResult("persona_runtime_fixture_valid", False, "persona_runtime.personas is required.")]

    report = {
        mode: _score_persona_runtime_mode(questions, payload)
        for mode, payload in personas.items()
    }
    fixture_type = str(runtime_eval.get("fixture_type") or case.get("suite") or "persona_runtime")
    report_summary = {
        "fixture_type": fixture_type,
        "modes": report,
        "winners": _runtime_winners(report),
    }
    assertions = case.get("assertions") or _default_runtime_assertions(report)
    results = [
        AssertionResult(
            "persona_runtime_metrics",
            True,
            "persona runtime metrics computed",
            expected={"fixture_type": fixture_type, "modes": sorted(personas)},
            actual=report_summary,
        )
    ]
    for assertion in assertions:
        if not isinstance(assertion, dict):
            results.append(AssertionResult("invalid", False, "Assertion must be an object.", expected=assertion))
            continue
        assertion_type = str(assertion.get("type") or "")
        try:
            results.append(_evaluate_persona_runtime_assertion(assertion_type, assertion, report_summary))
        except Exception as exc:
            results.append(AssertionResult(assertion_type or "invalid", False, str(exc), expected=assertion))
    return results


def _score_persona_runtime_mode(
        questions: list[dict[str, Any]],
        persona: dict[str, Any],
) -> dict[str, Any]:
    answers = {
        str(answer.get("question_id")): answer
        for answer in persona.get("answers") or []
        if isinstance(answer, dict) and answer.get("question_id")
    }
    per_question: list[dict[str, Any]] = []
    for question in questions:
        question_id = str(question.get("id") or "")
        answer = answers.get(question_id, {})
        text = str(answer.get("answer") or "")
        per_question.append(
            {
                "question_id": question_id,
                "answer_correctness": _keyword_score(text, question.get("expected_keywords")),
                "style_fidelity": _keyword_score(text, question.get("style_keywords")),
                "source_grounding": _source_grounding_score(text, answer, question),
                "uncertainty_refusal": _uncertainty_refusal_score(text, question),
                "graph_context_usefulness": _graph_context_score(text, answer, question),
            }
        )
    aggregate = {
        metric: _average([entry[metric] for entry in per_question])
        for metric in (
            "answer_correctness",
            "style_fidelity",
            "source_grounding",
            "uncertainty_refusal",
            "graph_context_usefulness",
        )
    }
    aggregate["overall"] = _average(list(aggregate.values()))
    aggregate["persona_version_id"] = persona.get("persona_version_id")
    aggregate["distillation_mode"] = persona.get("distillation_mode")
    aggregate["question_count"] = len(per_question)
    aggregate["questions"] = per_question
    return aggregate


def _keyword_score(text: str, keywords: Any) -> float:
    normalized_keywords = _normalized_keywords(keywords)
    if not normalized_keywords:
        return 1.0
    haystack = _normalized_text(text)
    return round(sum(1 for keyword in normalized_keywords if keyword in haystack) / len(normalized_keywords), 4)


def _source_grounding_score(text: str, answer: dict[str, Any], question: dict[str, Any]) -> float:
    expected_sources = _normalized_keywords(question.get("source_keywords"))
    citations = _normalized_keywords(answer.get("citations") or answer.get("source_citations") or [])
    if not expected_sources:
        return 1.0
    haystack = _normalized_text(" ".join([text, " ".join(citations)]))
    return round(sum(1 for keyword in expected_sources if keyword in haystack) / len(expected_sources), 4)


def _uncertainty_refusal_score(text: str, question: dict[str, Any]) -> float:
    if question.get("requires_uncertainty") is not True:
        return 1.0
    uncertainty_keywords = question.get("uncertainty_keywords") or ["not enough evidence", "cannot confirm", "source does not"]
    return _keyword_score(text, uncertainty_keywords)


def _graph_context_score(text: str, answer: dict[str, Any], question: dict[str, Any]) -> float:
    expected = _normalized_keywords(question.get("graph_context_keywords"))
    if not expected:
        return 1.0
    graph_context = answer.get("graph_context") if isinstance(answer.get("graph_context"), dict) else {}
    graph_terms = graph_context.get("used_nodes") or graph_context.get("used_edges") or []
    haystack = _normalized_text(" ".join([text, json.dumps(graph_terms, sort_keys=True, default=str)]))
    return round(sum(1 for keyword in expected if keyword in haystack) / len(expected), 4)


def _average(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(float(value) for value in values) / len(values), 4)


def _runtime_winners(report: dict[str, dict[str, Any]]) -> dict[str, str]:
    winners: dict[str, str] = {}
    for metric in (
        "overall",
        "answer_correctness",
        "style_fidelity",
        "source_grounding",
        "uncertainty_refusal",
        "graph_context_usefulness",
    ):
        winners[metric] = max(report.items(), key=lambda item: float(item[1].get(metric, 0.0)))[0]
    return winners


def _default_runtime_assertions(report: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    assertions = [
        {"type": "persona_runtime_metric_min", "mode": mode, "metric": "overall", "value": 0.7}
        for mode in sorted(report)
    ]
    if "hybrid" in report and "deterministic" in report:
        assertions.append(
            {"type": "persona_runtime_metric_gte", "mode": "hybrid", "baseline": "deterministic", "metric": "overall"}
        )
    return assertions


def _evaluate_persona_runtime_assertion(
        assertion_type: str,
        assertion: dict[str, Any],
        report_summary: dict[str, Any],
) -> AssertionResult:
    report = report_summary["modes"]
    if assertion_type == "persona_runtime_metric_min":
        mode = _required_assertion_string(assertion, "mode")
        metric = _required_assertion_string(assertion, "metric")
        expected = float(assertion.get("value") or 0.0)
        actual = float(report.get(mode, {}).get(metric, 0.0))
        return _result(assertion_type, actual >= expected, f"{mode}.{metric} meets minimum", expected, actual)
    if assertion_type == "persona_runtime_metric_gte":
        mode = _required_assertion_string(assertion, "mode")
        baseline = _required_assertion_string(assertion, "baseline")
        metric = _required_assertion_string(assertion, "metric")
        actual = float(report.get(mode, {}).get(metric, 0.0))
        expected = float(report.get(baseline, {}).get(metric, 0.0))
        return _result(assertion_type, actual >= expected, f"{mode}.{metric} is at least {baseline}.{metric}", expected, actual)
    if assertion_type == "persona_runtime_best_mode":
        mode = _required_assertion_string(assertion, "mode")
        metric = _required_assertion_string(assertion, "metric")
        actual = report_summary["winners"].get(metric)
        return _result(assertion_type, actual == mode, f"{mode} wins runtime {metric}", mode, actual)
    raise ValueError(f"Unsupported persona runtime assertion type: {assertion_type}")


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
    parser.add_argument(
        "--live-persona-distillation",
        action="store_true",
        help=(
            "Replay persona_distillation_fixture cases through a running backend. "
            "Requires --base-url and fixture source_memories."
        ),
    )
    parser.add_argument(
        "--live-persona-modes",
        default="deterministic,llm,hybrid",
        help="Comma-separated modes for --live-persona-distillation.",
    )
    parser.add_argument("--live-user-id", default="persona-eval-user", help="User id for live persona eval writes.")
    parser.add_argument(
        "--live-user-email",
        default="persona-eval-user@example.local",
        help="User email for live persona eval writes.",
    )
    parser.add_argument(
        "--live-llm-model-source",
        choices=("main_agent", "model_profile", "model"),
        help="Optional llm_model_source override for live LLM/hybrid persona evals.",
    )
    parser.add_argument("--live-model-profile-id", help="Optional model_profile_id for live persona evals.")
    parser.add_argument("--live-llm-model-provider", help="Optional provider for live llm_model_source=model evals.")
    parser.add_argument("--live-llm-model", help="Optional model for live llm_model_source=model evals.")
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
    live_persona_options = LivePersonaDistillationOptions(
        modes=_parse_live_persona_modes(args.live_persona_modes),
        user_id=args.live_user_id,
        user_email=args.live_user_email,
        llm_model_source=args.live_llm_model_source,
        model_profile_id=args.live_model_profile_id,
        llm_model_provider=args.live_llm_model_provider,
        llm_model=args.live_llm_model,
    )
    results = [
        run_case(
            case,
            base_url=args.base_url,
            judge_agent=args.judge_agent,
            timeout_seconds=args.timeout_seconds,
            live_persona_distillation=args.live_persona_distillation,
            live_persona_options=live_persona_options,
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


def _parse_live_persona_modes(value: str) -> tuple[str, ...]:
    modes = tuple(
        item.strip().lower()
        for item in str(value or "").split(",")
        if item.strip()
    )
    if not modes:
        raise ValueError("--live-persona-modes must include at least one mode.")
    unsupported = [mode for mode in modes if mode not in {"deterministic", "llm", "hybrid"}]
    if unsupported:
        raise ValueError(f"Unsupported --live-persona-modes value(s): {', '.join(unsupported)}")
    return modes


if __name__ == "__main__":
    raise SystemExit(main())
