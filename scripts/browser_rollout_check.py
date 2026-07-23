#!/usr/bin/env python3
"""Capture, compare, and verify browser-runtime rollout evidence.

Records contain status, timings, engine choice, challenge outcomes, cleanup, and
process-tree resource gauges. They intentionally omit page content and secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

try:
    from scripts._bootstrap import bootstrap_repo
except ModuleNotFoundError:  # Direct ``python scripts/...`` invocation.
    from _bootstrap import bootstrap_repo

bootstrap_repo(__file__, reexec=__name__ == "__main__")

from scripts.browser_live_check import run as run_live_check


SCHEMA = "agency.browser.rollout.v1"


def capture(
        *,
        label: str,
        urls: list[str],
        challenge_url: str | None = None,
        challenge_kind: str | None = None,
        human_wait_seconds: float = 0,
        human_poll_seconds: float = 2,
) -> dict[str, Any]:
    if not urls:
        raise ValueError("At least one approved --url is required")
    if challenge_url and human_wait_seconds <= 0:
        raise ValueError("A challenge rollout check requires --human-wait-seconds greater than zero")

    scenarios: list[dict[str, Any]] = []
    requested: list[tuple[str, str]] = [("normal", url) for url in urls]
    if challenge_url:
        requested.append(("challenge", challenge_url))
    for kind, url in requested:
        started = time.perf_counter()
        try:
            result = run_live_check(
                url,
                expect_challenge=kind == "challenge",
                challenge_kind=challenge_kind if kind == "challenge" else None,
                human_wait_seconds=human_wait_seconds if kind == "challenge" else 0,
                human_poll_seconds=human_poll_seconds,
            )
            success = _scenario_succeeded(kind, result)
            scenarios.append({
                "kind": kind,
                "url": url,
                "status": "passed" if success else "failed",
                "wall_time_ms": round((time.perf_counter() - started) * 1000, 3),
                "result": result,
            })
        except Exception as exc:
            scenarios.append({
                "kind": kind,
                "url": url,
                "status": "failed",
                "wall_time_ms": round((time.perf_counter() - started) * 1000, 3),
                "error_type": type(exc).__name__,
                "error": str(exc),
            })

    return {
        "schema": SCHEMA,
        "label": label,
        "captured_at": int(time.time()),
        "runtime_url": os.getenv("BROWSER_RUNTIME_URL", "http://127.0.0.1:8010"),
        "scenarios": scenarios,
        "summary": summarize(scenarios),
    }


def summarize(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    sample_count = len(scenarios)
    successes = sum(item.get("status") == "passed" for item in scenarios)
    wall_times = [float(item.get("wall_time_ms", 0)) for item in scenarios]
    fallback_count = 0
    challenge_count = 0
    recovered_count = 0
    cleanup_failures = 0
    rss_samples: list[int] = []
    pid_samples: list[int] = []
    resource_sources: set[str] = set()
    releases: list[dict[str, str]] = []

    for scenario in scenarios:
        result = scenario.get("result") or {}
        sessionless = result.get("sessionless") or {}
        retained = result.get("retained") or {}
        if "scrapling" in {sessionless.get("engine"), retained.get("engine")}:
            fallback_count += 1
        if sessionless.get("challenge") not in {None, "none"}:
            challenge_count += 1
            if retained.get("challenge_recovered"):
                recovered_count += 1
        cleanup = result.get("cleanup") or {}
        if cleanup and (
                not cleanup.get("closed")
                or cleanup.get("owner_sessions") != 0
                or int(cleanup.get("cleanup_failures") or 0) > 0
        ):
            cleanup_failures += 1
        resources = result.get("resources") or {}
        memory_value = resources.get("cgroup_memory_current_bytes")
        pid_value = resources.get("cgroup_pids_current")
        if memory_value is not None and pid_value is not None:
            resource_sources.add("cgroup")
        else:
            memory_value = resources.get("process_tree_rss_bytes")
            pid_value = resources.get("process_tree_pids")
            resource_sources.add("process_tree")
        if memory_value is not None:
            rss_samples.append(int(memory_value))
        if pid_value is not None:
            pid_samples.append(int(pid_value))
        release = (result.get("health") or {}).get("release")
        if release and release not in releases:
            releases.append(release)

    return {
        "sample_count": sample_count,
        "success_count": successes,
        "success_rate": successes / sample_count if sample_count else 0.0,
        "latency_ms": {
            "mean": round(statistics.fmean(wall_times), 3) if wall_times else 0.0,
            "max": round(max(wall_times), 3) if wall_times else 0.0,
        },
        "fallback_count": fallback_count,
        "fallback_rate": fallback_count / sample_count if sample_count else 0.0,
        "challenge_count": challenge_count,
        "challenge_rate": challenge_count / sample_count if sample_count else 0.0,
        "challenge_recovery_rate": recovered_count / challenge_count if challenge_count else 1.0,
        "cleanup_failure_count": cleanup_failures,
        "max_memory_bytes": max(rss_samples, default=None),
        "max_pids": max(pid_samples, default=None),
        "resource_sources": sorted(resource_sources),
        "releases": releases,
    }


def merge_records(records: list[dict[str, Any]], *, label: str) -> dict[str, Any]:
    if not records:
        raise ValueError("At least one rollout record is required")
    scenarios: list[dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        _validate_record(record, f"record {index}")
        scenarios.extend(record["scenarios"])
    return {
        "schema": SCHEMA,
        "label": label,
        "captured_at": int(time.time()),
        "runtime_url": records[0].get("runtime_url"),
        "scenarios": scenarios,
        "summary": summarize(scenarios),
    }


def compare_records(
        baseline: dict[str, Any],
        candidate: dict[str, Any],
        *,
        expected_release: str | None = None,
        max_success_rate_drop: float = 0.02,
        max_latency_ratio: float = 1.5,
        max_resource_ratio: float = 1.5,
        max_fallback_rate_increase: float = 0.1,
        max_challenge_rate_increase: float = 0.1,
) -> dict[str, Any]:
    _validate_record(baseline, "baseline")
    _validate_record(candidate, "candidate")
    before = baseline["summary"]
    after = candidate["summary"]
    checks: list[dict[str, Any]] = []

    baseline_targets = [(item.get("kind"), item.get("url")) for item in baseline["scenarios"]]
    candidate_targets = [(item.get("kind"), item.get("url")) for item in candidate["scenarios"]]
    _check(checks, "same_scenarios", candidate_targets == baseline_targets, baseline_targets, candidate_targets)
    _check(
        checks,
        "same_resource_source",
        after.get("resource_sources") == before.get("resource_sources"),
        before.get("resource_sources"),
        after.get("resource_sources"),
    )
    _check(
        checks,
        "success_rate",
        float(after["success_rate"]) >= float(before["success_rate"]) - max_success_rate_drop,
        f">={float(before['success_rate']) - max_success_rate_drop:.4f}",
        after["success_rate"],
    )
    _ratio_check(checks, "mean_latency_ms", before["latency_ms"]["mean"], after["latency_ms"]["mean"], max_latency_ratio)
    _ratio_check(
        checks,
        "memory_bytes",
        before.get("max_memory_bytes"),
        after.get("max_memory_bytes"),
        max_resource_ratio,
    )
    _ratio_check(
        checks,
        "pids",
        before.get("max_pids"),
        after.get("max_pids"),
        max_resource_ratio,
    )
    _check(
        checks,
        "fallback_rate",
        float(after["fallback_rate"]) <= float(before["fallback_rate"]) + max_fallback_rate_increase,
        f"<={float(before['fallback_rate']) + max_fallback_rate_increase:.4f}",
        after["fallback_rate"],
    )
    _check(
        checks,
        "challenge_rate",
        float(after["challenge_rate"]) <= float(before["challenge_rate"]) + max_challenge_rate_increase,
        f"<={float(before['challenge_rate']) + max_challenge_rate_increase:.4f}",
        after["challenge_rate"],
    )
    _check(
        checks,
        "challenge_recovery_rate",
        float(after["challenge_recovery_rate"]) >= float(before["challenge_recovery_rate"]),
        f">={float(before['challenge_recovery_rate']):.4f}",
        after["challenge_recovery_rate"],
    )
    _check(checks, "cleanup_failures", int(after["cleanup_failure_count"]) == 0, 0, after["cleanup_failure_count"])
    if expected_release:
        release_ids = {str(item["id"]) for item in after.get("releases", []) if item.get("id")}
        _check(checks, "expected_release", release_ids == {expected_release}, [expected_release], sorted(release_ids))

    return {
        "schema": SCHEMA,
        "status": "passed" if all(item["passed"] for item in checks) else "failed",
        "baseline": baseline.get("label"),
        "candidate": candidate.get("label"),
        "checks": checks,
    }


def _scenario_succeeded(kind: str, result: dict[str, Any]) -> bool:
    cleanup = result.get("cleanup") or {}
    clean = cleanup.get("closed") is True and cleanup.get("owner_sessions") == 0
    retained = result.get("retained") or {}
    sessionless = result.get("sessionless") or {}
    if kind == "challenge":
        return (
            sessionless.get("challenge") not in {None, "none"}
            and retained.get("challenge_recovered") is True
            and retained.get("status") == "ok"
            and clean
        )
    return sessionless.get("status") == "ok" and retained.get("status") == "ok" and clean


def _validate_record(record: dict[str, Any], label: str) -> None:
    if record.get("schema") != SCHEMA or not isinstance(record.get("summary"), dict):
        raise ValueError(f"{label} is not a {SCHEMA} rollout record")


def _check(checks: list[dict[str, Any]], name: str, passed: bool, expected: Any, actual: Any) -> None:
    checks.append({"name": name, "passed": bool(passed), "expected": expected, "actual": actual})


def _ratio_check(checks: list[dict[str, Any]], name: str, before: Any, after: Any, ratio: float) -> None:
    if before is None or after is None:
        _check(checks, name, False, "resource metric present in both records", {"baseline": before, "candidate": after})
        return
    maximum = float(before) * ratio
    _check(checks, name, float(after) <= maximum, f"<={maximum:.3f}", after)


def _load(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def _write(payload: dict[str, Any], path: str | None) -> None:
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    if path:
        Path(path).write_text(serialized + "\n")
    print(serialized)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture and compare approved browser-runtime rollout evidence.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture_parser = subparsers.add_parser("capture", help="Run approved live scenarios and write a rollout record.")
    capture_parser.add_argument("--label", required=True)
    capture_parser.add_argument("--url", action="append", required=True, dest="urls")
    capture_parser.add_argument("--challenge-url")
    capture_parser.add_argument("--challenge-kind")
    capture_parser.add_argument("--human-wait-seconds", type=float, default=0)
    capture_parser.add_argument("--human-poll-seconds", type=float, default=2)
    capture_parser.add_argument("--output")

    compare_parser = subparsers.add_parser("compare", help="Gate a candidate record against its baseline.")
    compare_parser.add_argument("--baseline", required=True)
    compare_parser.add_argument("--candidate", required=True)
    compare_parser.add_argument("--expected-release")
    compare_parser.add_argument("--max-success-rate-drop", type=float, default=0.02)
    compare_parser.add_argument("--max-latency-ratio", type=float, default=1.5)
    compare_parser.add_argument("--max-resource-ratio", type=float, default=1.5)
    compare_parser.add_argument("--max-fallback-rate-increase", type=float, default=0.1)
    compare_parser.add_argument("--max-challenge-rate-increase", type=float, default=0.1)
    compare_parser.add_argument("--output")

    merge_parser = subparsers.add_parser("merge", help="Combine repeated rollout records into one sample set.")
    merge_parser.add_argument("--record", action="append", required=True, dest="records")
    merge_parser.add_argument("--label", required=True)
    merge_parser.add_argument("--output")

    rollback_parser = subparsers.add_parser(
        "verify-rollback",
        help="Prove a rollback restored the expected release and baseline behavior.",
    )
    rollback_parser.add_argument("--baseline", required=True)
    rollback_parser.add_argument("--rollback", required=True)
    rollback_parser.add_argument("--expected-release", required=True)
    rollback_parser.add_argument("--output")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "capture":
            payload = capture(
                label=args.label,
                urls=args.urls,
                challenge_url=args.challenge_url,
                challenge_kind=args.challenge_kind,
                human_wait_seconds=args.human_wait_seconds,
                human_poll_seconds=args.human_poll_seconds,
            )
            _write(payload, args.output)
            return 0 if payload["summary"]["success_rate"] == 1.0 else 1
        if args.command == "merge":
            payload = merge_records([_load(path) for path in args.records], label=args.label)
            _write(payload, args.output)
            return 0 if payload["summary"]["success_rate"] == 1.0 else 1
        if args.command == "compare":
            payload = compare_records(
                _load(args.baseline),
                _load(args.candidate),
                expected_release=args.expected_release,
                max_success_rate_drop=args.max_success_rate_drop,
                max_latency_ratio=args.max_latency_ratio,
                max_resource_ratio=args.max_resource_ratio,
                max_fallback_rate_increase=args.max_fallback_rate_increase,
                max_challenge_rate_increase=args.max_challenge_rate_increase,
            )
        else:
            payload = compare_records(
                _load(args.baseline),
                _load(args.rollback),
                expected_release=args.expected_release,
            )
        _write(payload, args.output)
        return 0 if payload["status"] == "passed" else 1
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

