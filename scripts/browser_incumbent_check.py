#!/usr/bin/env python3
"""Capture a rollout-compatible baseline from Agency's incumbent browser code."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any


SCHEMA = "agency.browser.rollout.v1"


def _process_tree_resources(root_pid: int) -> dict[str, int]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,rss=,state=,comm="],
        check=True,
        capture_output=True,
        text=True,
    )
    parents: dict[int, int] = {}
    rss_kib: dict[int, int] = {}
    for line in result.stdout.splitlines():
        try:
            pid_text, parent_text, rss_text, state, command = line.split(maxsplit=4)
            # The measurement command is briefly our child and would otherwise
            # look like a leaked browser process after Playwright shutdown.
            if Path(command).name == "ps" or state.startswith("Z"):
                continue
            pid = int(pid_text)
            parents[pid] = int(parent_text)
            rss_kib[pid] = int(rss_text)
        except (ValueError, TypeError):
            continue
    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if parent in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    measured = descendants.intersection(rss_kib)
    return {
        "process_tree_pids": len(measured),
        "process_tree_rss_bytes": sum(rss_kib[pid] for pid in measured) * 1024,
    }


def _cgroup_resources() -> dict[str, int] | None:
    values: dict[str, int] = {}
    for name, candidates in {
        "cgroup_memory_current_bytes": (
            Path("/sys/fs/cgroup/memory.current"),
            Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
        ),
        "cgroup_pids_current": (
            Path("/sys/fs/cgroup/pids.current"),
            Path("/sys/fs/cgroup/pids/pids.current"),
        ),
    }.items():
        for candidate in candidates:
            try:
                values[name] = int(candidate.read_text().strip())
                break
            except (OSError, ValueError):
                continue
    return values if len(values) == 2 else None


def capture(*, repo_root: Path, url: str, label: str, output_root: Path) -> dict[str, Any]:
    if not repo_root.joinpath("app/tools/implementations/browser.py").is_file():
        raise ValueError(f"Incumbent repository is missing browser.py: {repo_root}")
    output_root.mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "APP_ENV": "development",
        "ENVIRONMENT": "local",
        "BROWSER_RUNTIME_ROOT": str(output_root / "runtime"),
        "LOCAL_STORAGE_PATH": str(output_root / "storage"),
    })
    sys.path.insert(0, str(repo_root))
    # Loading a historical implementation should not import every unrelated
    # tool from that revision's eager package __init__ (and all its optional
    # dependencies). Namespace stubs preserve normal relative imports while
    # keeping the baseline probe scoped to browser code.
    for package_name, package_path in (
        ("app", repo_root / "app"),
        ("app.tools", repo_root / "app/tools"),
        ("app.tools.implementations", repo_root / "app/tools/implementations"),
    ):
        package = types.ModuleType(package_name)
        setattr(package, "__path__", [str(package_path)])
        sys.modules[package_name] = package

    from app.tools.implementations.browser import (  # pylint: disable=import-outside-toplevel
        open_browser,
        screenshot_and_extract,
        scroll_page,
        terminate_browser,
        verify_content,
    )

    started = time.perf_counter()
    resources: dict[str, int] = {}
    result: dict[str, Any] = {}
    error: Exception | None = None
    try:
        opened = open_browser(
            url=url,
            headless_mode=True,
            trace_mode="off",
            _allowed_hosts=[_host(url)],
        )
        resources = _cgroup_resources() or _process_tree_resources(os.getpid())
        verification = verify_content("Example Domain")
        scroll = scroll_page("down 1")
        extraction = screenshot_and_extract("Extract the visible article text")
        content = (extraction.get("content") or {}).get("text") if isinstance(extraction, dict) else None
        score = int(verification.get("Verification Score", 0)) if isinstance(verification, dict) else 0
        if not isinstance(opened, dict) or opened.get("title") != "Example Domain":
            raise AssertionError(f"Incumbent open failed: {opened}")
        if score != 100 or not content or "Example Domain" not in content:
            raise AssertionError("Incumbent extraction or verification failed")
        if "Scrolled" not in str(scroll):
            raise AssertionError("Incumbent retained interaction failed")
        result = {
            "health": {"before": "ok", "after": "ok", "release": {"id": label, "image": "git-head"}},
            # The incumbent has no sessionless lifecycle; its retained screenshot
            # extraction is represented as the comparable retrieval outcome.
            "sessionless": {
                "status": "ok",
                "engine": "playwright-incumbent",
                "interactive": True,
                "challenge": opened.get("challenge_detected") or "none",
            },
            "retained": {
                "status": "ok",
                "engine": "playwright-incumbent",
                "interactive": True,
                "refreshed": True,
                "challenge_recovered": False,
            },
            "cleanup": {"closed": True, "owner_sessions": 0, "cleanup_failures": 0},
            "resources": resources,
        }
    except Exception as exc:  # Preserve a machine-readable failed baseline.
        error = exc
    finally:
        try:
            terminate_browser()
        except Exception as exc:
            error = error or exc

    wall_time_ms = round((time.perf_counter() - started) * 1000, 3)
    remaining = _process_tree_resources(os.getpid())["process_tree_pids"] - 1
    success = error is None and remaining == 0
    if result:
        result["cleanup"]["closed"] = remaining == 0
        result["cleanup"]["remaining_child_processes"] = remaining
    scenario = {
        "kind": "normal",
        "url": url,
        "status": "passed" if success else "failed",
        "wall_time_ms": wall_time_ms,
        **({"result": result} if result else {}),
        **({"error_type": type(error).__name__, "error": str(error)} if error else {}),
    }
    summary = {
        "sample_count": 1,
        "success_count": int(success),
        "success_rate": float(success),
        "latency_ms": {"mean": wall_time_ms, "max": wall_time_ms},
        "fallback_count": 0,
        "fallback_rate": 0.0,
        "challenge_count": 0,
        "challenge_rate": 0.0,
        "challenge_recovery_rate": 1.0,
        "cleanup_failure_count": 0 if success else 1,
        "max_memory_bytes": resources.get("cgroup_memory_current_bytes", resources.get("process_tree_rss_bytes")),
        "max_pids": resources.get("cgroup_pids_current", resources.get("process_tree_pids")),
        "resource_sources": ["cgroup" if "cgroup_memory_current_bytes" in resources else "process_tree"],
        "releases": [{"id": label, "image": "git-head"}],
    }
    return {
        "schema": SCHEMA,
        "label": label,
        "captured_at": int(time.time()),
        "runtime_url": "in-process-incumbent",
        "scenarios": [scenario],
        "summary": summary,
    }


def _host(url: str) -> str:
    from urllib.parse import urlsplit
    return urlsplit(url).hostname or ""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture the incumbent Agency browser as a rollout baseline.")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        payload = capture(
            repo_root=args.repo_root.resolve(),
            url=args.url,
            label=args.label,
            output_root=args.output_root.resolve(),
        )
        serialized = json.dumps(payload, indent=2, sort_keys=True)
        if args.output:
            args.output.write_text(serialized + "\n")
        print(serialized)
        return 0 if payload["summary"]["success_rate"] == 1.0 else 1
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

