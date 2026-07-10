#!/usr/bin/env python3
"""Smoke-test native/CrewAI runtime execution paths against a running backend."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from typing import Any
from uuid import uuid4

try:
    from scripts._bootstrap import bootstrap_repo
except ModuleNotFoundError:
    from _bootstrap import bootstrap_repo

bootstrap_repo(__file__, reexec=__name__ == "__main__")

import httpx


def _read_sse_data(line: str) -> str | None:
    if not line.startswith("data: "):
        return None
    return line[6:]


def ensure_model_provider(
        client: httpx.Client,
        *,
        provider: str,
        base_url: str | None,
) -> None:
    payload = {
        "id": provider,
        "name": provider.replace("_", " ").title(),
        "provider_type": provider,
        "endpoint": {"base_url": base_url} if base_url else {"base_url": None},
        "config": {},
    }
    response = client.post("/model-providers", json=payload)
    if response.status_code in {200, 409, 422}:
        return
    response.raise_for_status()


def build_workflow_payload(
        *,
        workflow_id: str,
        runtime: str,
        provider: str,
        model: str,
        base_url: str | None,
        api_key: str | None,
        exercise_hitl: bool,
) -> dict[str, Any]:
    agent_id = f"{workflow_id}-agent"
    task_id = f"{workflow_id}-task"
    node_id = f"{workflow_id}-node"
    profile_id = f"{workflow_id}-profile"
    task_description = "Return a concise completion."
    task_expected_output = "A concise completion."
    agent_instructions = "Answer the task clearly and concisely."
    task_tool_ids: list[str] = []
    agent_tool_ids: list[str] = []
    if exercise_hitl:
        task_description = (
            "Before giving any final answer, call the Human: Ask Operator tool exactly once with the exact question "
            "'Reply with exactly: HITL validation acknowledged'. After the human responds, return exactly the "
            "human reply and nothing else."
        )
        task_expected_output = "Exactly the human reply."
        agent_instructions = (
            "You must use the Human: Ask Operator tool before answering. Ask exactly: "
            "'Reply with exactly: HITL validation acknowledged'. After receiving a response, return exactly that "
            "response and nothing else."
        )
        task_tool_ids = ["agency.human.ask"]
        agent_tool_ids = ["agency.human.ask"]
    workflow = {
        "id": workflow_id,
        "name": f"{runtime.title()} Validation Workflow",
        "entrypoint": node_id,
        "nodes": [
            {
                "id": node_id,
                "name": "Validation Node",
                "node_type": "task",
                "task_id": task_id,
                "agent_id": agent_id,
            }
        ],
        "task_definitions": [
            {
                "id": task_id,
                "name": "Validation Task",
                "description": task_description,
                "expected_output": task_expected_output,
                "agent_id": agent_id,
                "tool_ids": task_tool_ids,
            }
        ],
        "agent_definitions": [
            {
                "id": agent_id,
                "name": "Validation Agent",
                "role": "Operator",
                "instructions": agent_instructions,
                "model_profile_id": profile_id,
                "tool_ids": agent_tool_ids,
            }
        ],
        "default_runtime_adapter_id": runtime,
        "allowed_runtime_adapter_ids": ["native", "crewai"],
    }
    profile: dict[str, Any] = {
        "id": profile_id,
        "name": f"{runtime.title()} Validation Profile",
        "provider": provider,
        "model": model,
        "supports_tools": exercise_hitl,
    }
    if base_url:
        profile["base_url"] = base_url
    if api_key:
        profile["api_key_ref"] = api_key
    return {
        "workflow_definition": workflow,
        "model_profiles": [profile],
    }


def _capture_execution_events(base_url: str, execution_id: str, timeout_seconds: float,
                              sink: list[dict[str, Any]]) -> None:
    deadline = time.time() + timeout_seconds
    timeout = httpx.Timeout(timeout_seconds, connect=10.0)
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout) as client:
        with client.stream("GET", f"/executions/{execution_id}/stream") as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if time.time() >= deadline:
                    break
                if not line:
                    continue
                payload_text = _read_sse_data(line)
                if payload_text is None:
                    continue
                payload = json.loads(payload_text)
                sink.append(payload)
                if payload.get("event_type") in {"execution.completed", "execution.failed", "execution.cancelled"}:
                    break


def _wait_for_hitl_prompt(base_url: str, execution_id: str, timeout_seconds: float, holder: dict[str, Any]) -> None:
    deadline = time.time() + timeout_seconds
    timeout = httpx.Timeout(timeout_seconds, connect=10.0)
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout) as client:
        with client.stream("GET", f"/executions/{execution_id}/hitl/stream") as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if time.time() >= deadline:
                    break
                if not line:
                    continue
                payload_text = _read_sse_data(line)
                if payload_text is None:
                    continue
                holder["prompt"] = payload_text
                break


def poll_execution(client: httpx.Client, *, execution_id: str, timeout_seconds: float) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        response = client.get(f"/executions/{execution_id}")
        response.raise_for_status()
        payload = response.json()
        status = payload["execution"]["status"]
        if status in {"completed", "failed", "cancelled"}:
            return payload
        time.sleep(0.25)
    response = client.get(f"/executions/{execution_id}")
    response.raise_for_status()
    return response.json()


def _exercise_hitl(
        client: httpx.Client,
        *,
        base_url: str,
        execution_id: str,
        timeout_seconds: float,
        reply: str,
) -> dict[str, Any]:
    prompt_holder: dict[str, Any] = {"prompt": None}
    hitl_thread = threading.Thread(
        target=_wait_for_hitl_prompt,
        kwargs={
            "base_url": base_url,
            "execution_id": execution_id,
            "timeout_seconds": timeout_seconds,
            "holder": prompt_holder,
        },
        daemon=True,
    )
    hitl_thread.start()
    deadline = time.time() + timeout_seconds
    while time.time() < deadline and prompt_holder["prompt"] is None:
        time.sleep(0.1)
    if prompt_holder["prompt"] is None:
        hitl_thread.join(timeout=1)
        raise RuntimeError("Timed out waiting for HITL prompt")
    reply_response = client.post(f"/executions/{execution_id}/hitl/reply", json={"reply": reply})
    reply_response.raise_for_status()
    hitl_thread.join(timeout=1)
    return {
        "prompt": prompt_holder["prompt"],
        "reply": reply,
    }


def validate_runtime(
        client: httpx.Client,
        *,
        base_url: str,
        runtime: str,
        provider: str,
        model: str,
        model_base_url: str | None,
        api_key: str | None,
        timeout_seconds: float,
        exercise_hitl: bool,
        hitl_reply: str,
) -> dict[str, Any]:
    workflow_id = f"workflow-{runtime}-validation-{uuid4().hex[:8]}"
    ensure_model_provider(client, provider=provider, base_url=model_base_url)
    payload = build_workflow_payload(
        workflow_id=workflow_id,
        runtime=runtime,
        provider=provider,
        model=model,
        base_url=model_base_url,
        api_key=api_key,
        exercise_hitl=exercise_hitl,
    )
    start_response = client.post(
        f"/workflows/{workflow_id}/executions/start",
        json={
            "input": {"topic": f"{runtime} validation"},
            "trigger": {"created_by": "validate_runtime_paths"},
            "runtimeAdapterId": runtime,
            **payload,
        },
    )
    start_response.raise_for_status()
    start_payload = start_response.json()
    execution_id = start_payload["execution"]["id"]
    streamed_events: list[dict[str, Any]] = []
    event_thread = threading.Thread(
        target=_capture_execution_events,
        kwargs={
            "base_url": base_url,
            "execution_id": execution_id,
            "timeout_seconds": timeout_seconds,
            "sink": streamed_events,
        },
        daemon=True,
    )
    event_thread.start()
    hitl_result: dict[str, Any] | None = None
    if exercise_hitl:
        hitl_result = _exercise_hitl(
            client,
            base_url=base_url,
            execution_id=execution_id,
            timeout_seconds=timeout_seconds,
            reply=hitl_reply,
        )
    final_payload = poll_execution(client, execution_id=execution_id, timeout_seconds=timeout_seconds)
    event_thread.join(timeout=1)

    events_response = client.get(f"/executions/{execution_id}/events")
    events_response.raise_for_status()
    artifacts_response = client.get(f"/executions/{execution_id}/artifacts")
    artifacts_response.raise_for_status()

    events = events_response.json()["items"]
    artifacts = artifacts_response.json()["items"]
    execution = final_payload["execution"]
    return {
        "runtime": runtime,
        "execution_id": execution_id,
        "start_status_code": start_response.status_code,
        "final_status": execution["status"],
        "output_payload": execution.get("output_payload"),
        "error": execution.get("error"),
        "streamed_event_types": [item["event_type"] for item in streamed_events],
        "event_types": [item["event_type"] for item in events],
        "artifact_names": [item["name"] for item in artifacts],
        "hitl": hitl_result,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate canonical workflow execution paths against a running Agency backend.")
    parser.add_argument("--base-url", default="http://localhost:8000",
                        help="Backend base URL. Default: http://localhost:8000")
    parser.add_argument("--runtime", choices=["native", "crewai", "both"], default="both",
                        help="Which runtime path to validate.")
    parser.add_argument("--provider", default="ollama",
                        help="Model provider key to place in the temporary model profile.")
    parser.add_argument("--model", default="llama3:8b", help="Model name for the temporary model profile.")
    parser.add_argument("--model-base-url", default=None,
                        help=(
                            "Optional model endpoint base URL to place in the temporary model profile. "
                            "For host Ollama with a Dockerized backend, prefer http://host.docker.internal:11434."
                        ))
    parser.add_argument("--api-key", default=None,
                        help="Optional API key to place in the temporary model profile as api_key_ref.")
    parser.add_argument("--timeout-seconds", type=float, default=30.0, help="Polling timeout per execution.")
    parser.add_argument("--exercise-hitl", action="store_true",
                        help="Run a CrewAI validation that uses the HITL stream/reply path.")
    parser.add_argument("--hitl-reply", default="HITL validation acknowledged",
                        help="Reply payload to send when exercising HITL.")
    parser.add_argument("--json", action="store_true", help="Print only JSON output.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runtimes = ["native", "crewai"] if args.runtime == "both" else [args.runtime]
    results: list[dict[str, Any]] = []
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=30.0) as client:
        for runtime in runtimes:
            exercise_hitl = args.exercise_hitl and runtime == "crewai"
            results.append(
                validate_runtime(
                    client,
                    base_url=args.base_url,
                    runtime=runtime,
                    provider=args.provider,
                    model=args.model,
                    model_base_url=args.model_base_url,
                    api_key=args.api_key,
                    timeout_seconds=args.timeout_seconds,
                    exercise_hitl=exercise_hitl,
                    hitl_reply=args.hitl_reply,
                )
            )

    has_failure = any(item["final_status"] != "completed" for item in results)
    if args.json:
        print(json.dumps({"results": results}, indent=2))
    else:
        print(json.dumps({"results": results}, indent=2))
        if has_failure:
            print("\nOne or more runtime validations did not complete successfully.", file=sys.stderr)

    return 1 if has_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
