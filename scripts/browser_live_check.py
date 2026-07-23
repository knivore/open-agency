#!/usr/bin/env python3
"""Opt-in browser-runtime validation against one explicitly approved URL."""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from typing import Any

try:
    from scripts._bootstrap import bootstrap_repo
except ModuleNotFoundError:  # Direct ``python scripts/...`` invocation.
    from _bootstrap import bootstrap_repo

bootstrap_repo(__file__, reexec=__name__ == "__main__")

from app.browser_runtime.client import BrowserRuntimeClient
from app.browser_runtime.contracts import BrowserOptions, OwnerClaims


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate sessionless and retained Agency browser flows against an approved public URL.",
    )
    parser.add_argument("--url", required=True, help="Explicit public URL approved for this live check.")
    parser.add_argument(
        "--expect-challenge",
        action="store_true",
        help="Require structured challenge detection instead of ordinary extracted content.",
    )
    parser.add_argument(
        "--challenge-kind",
        help="Optional exact challenge kind expected when --expect-challenge is set.",
    )
    parser.add_argument(
        "--human-wait-seconds",
        type=float,
        default=0,
        help=(
            "Wait this long for an operator to complete a retained challenge, then prove that the same session "
            "resumes. Zero records handoff without waiting."
        ),
    )
    parser.add_argument(
        "--human-poll-seconds",
        type=float,
        default=2,
        help="Polling interval while waiting for human challenge completion.",
    )
    return parser


def run(
        url: str,
        *,
        expect_challenge: bool,
        challenge_kind: str | None,
        human_wait_seconds: float = 0,
        human_poll_seconds: float = 2,
) -> dict[str, Any]:
    if human_wait_seconds < 0:
        raise ValueError("human_wait_seconds cannot be negative")
    if human_poll_seconds <= 0:
        raise ValueError("human_poll_seconds must be positive")
    client = BrowserRuntimeClient()
    owner = OwnerClaims(execution_id=f"browser-live-check-{uuid.uuid4().hex}")
    retained_session: str | None = None
    try:
        health_before = client.health()
        sessionless = client.open(
            url=url,
            owner=owner,
            extract_mode="article",
            keep_open=False,
            options=BrowserOptions(trace_mode="on"),
            correlation_id=owner.execution_id,
        )
        if expect_challenge:
            detected = sessionless.get("challenge", {}).get("kind")
            if detected == "none" or (challenge_kind and detected != challenge_kind):
                raise AssertionError(f"Expected challenge {challenge_kind or 'any'}, received {detected}")
        else:
            if sessionless.get("status") != "ok" or not sessionless.get("extraction"):
                raise AssertionError(f"Sessionless extraction failed: {sessionless.get('diagnostics')}")

        retained = client.open(
            url=url,
            owner=owner,
            extract_mode="markdown",
            keep_open=True,
            correlation_id=owner.execution_id,
        )
        retained_session = retained.get("session_id")
        if retained.get("status") == "human_action_required":
            if not retained_session:
                raise AssertionError("Human handoff did not preserve a session")
            retained_challenge_kind = (retained.get("challenge") or {}).get("kind")
            if challenge_kind and retained_challenge_kind != challenge_kind:
                raise AssertionError(
                    f"Expected retained challenge {challenge_kind}, received {retained_challenge_kind}"
                )
            handoff = retained.get("human_handoff") or {}
            retained_result = {
                "status": retained["status"],
                "challenge": retained.get("challenge"),
                "human_handoff": handoff,
                "challenge_recovered": False,
                "timings": retained.get("timings", {}),
            }
            if human_wait_seconds:
                print(
                    json.dumps(
                        {
                            "event": "human_action_required",
                            "session_id": retained_session,
                            "instructions": handoff.get("instructions"),
                            "expires_at": handoff.get("expires_at"),
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                    flush=True,
                )
                deadline = time.monotonic() + human_wait_seconds
                while True:
                    resumed = client.extract(retained_session, owner=owner, extract_mode="auto")
                    resumed_challenge = (resumed.get("challenge") or {}).get("kind", "none")
                    if resumed.get("status") == "ok" and resumed_challenge == "none":
                        if resumed.get("session_id") != retained_session or not resumed.get("extraction"):
                            raise AssertionError("Challenge recovery did not resume the retained session")
                        retained_result = {
                            "status": "ok",
                            "engine": retained.get("engine"),
                            "interactive": True,
                            "human_handoff": True,
                            "challenge_recovered": True,
                            "initial_challenge": (retained.get("challenge") or {}).get("kind"),
                            "timings": resumed.get("timings", {}),
                        }
                        break
                    if time.monotonic() >= deadline:
                        raise AssertionError(
                            f"Human challenge was not cleared within {human_wait_seconds:g} seconds"
                        )
                    time.sleep(min(human_poll_seconds, max(0, deadline - time.monotonic())))
        else:
            if retained.get("status") != "ok" or not retained.get("interactive") or not retained_session:
                raise AssertionError(f"Retained open failed: {retained.get('diagnostics')}")
            client.action(retained_session, owner=owner, action="scroll", scroll_direction="down 1")
            refreshed = client.extract(retained_session, owner=owner, extract_mode="auto")
            if refreshed.get("session_id") != retained_session or not refreshed.get("extraction"):
                raise AssertionError("Repeated extraction did not preserve the retained session")
            retained_result = {
                "status": "ok",
                "engine": retained.get("engine"),
                "interactive": True,
                "refreshed": True,
                "challenge_recovered": expect_challenge,
                "recovery_mode": "fresh_retained_navigation" if expect_challenge else None,
                "timings": retained.get("timings", {}),
            }

        # Sample while the retained Chromium page is alive so rollout records
        # include the browser process tree rather than only idle runtime cost.
        health_during = client.health()
        close_result = client.close(retained_session, owner=owner) if retained_session else {"closed": True}
        if retained_session and not close_result.get("closed"):
            raise AssertionError("Retained session did not close")
        retained_session = None
        remaining = client.status(owner=owner).get("sessions", [])
        if remaining:
            raise AssertionError(f"Live check leaked {len(remaining)} owner session(s)")
        health_after = client.health()

        return {
            "url": url,
            "health": {
                "before": health_before.get("status"),
                "after": health_after.get("status"),
                "release": health_after.get("release", {}),
            },
            "sessionless": {
                "status": sessionless.get("status"),
                "engine": sessionless.get("engine"),
                "interactive": sessionless.get("interactive"),
                "challenge": sessionless.get("challenge", {}).get("kind"),
                "artifacts": sorted(sessionless.get("artifacts", {})),
                "timings": sessionless.get("timings", {}),
            },
            "retained": retained_result,
            "cleanup": {
                "closed": bool(close_result.get("closed")),
                "owner_sessions": 0,
                "active_sessions": health_after.get("active_sessions"),
                "cleanup_failures": health_after.get("metrics", {}).get("gauges", {}).get("cleanup_failures"),
            },
            "resources": health_during.get("metrics", {}).get("gauges", {}),
        }
    finally:
        if retained_session:
            try:
                client.close(retained_session, owner=owner)
            except Exception:
                pass
        client.close_client()


def main() -> int:
    args = _parser().parse_args()
    try:
        result = run(
            args.url,
            expect_challenge=args.expect_challenge,
            challenge_kind=args.challenge_kind,
            human_wait_seconds=args.human_wait_seconds,
            human_poll_seconds=args.human_poll_seconds,
        )
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"status": "passed", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

