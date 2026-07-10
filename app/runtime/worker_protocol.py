"""Exit codes and heartbeat constants shared by runtime workers and control plane."""

from __future__ import annotations

WORKER_EXIT_SUCCESS = 0
WORKER_EXIT_WORKFLOW_FAILED = 10
WORKER_EXIT_BOOTSTRAP_FAILED = 20
WORKER_EXIT_INFRA_FAILED = 30
WORKER_EXIT_CANCELLED = 40
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 1.0


def worker_exit_reason(exit_code: int | None) -> str:
    if exit_code == WORKER_EXIT_SUCCESS:
        return "worker_exit_success"
    if exit_code == WORKER_EXIT_WORKFLOW_FAILED:
        return "worker_exit_workflow_failed"
    if exit_code == WORKER_EXIT_BOOTSTRAP_FAILED:
        return "worker_exit_bootstrap_failed"
    if exit_code == WORKER_EXIT_INFRA_FAILED:
        return "worker_exit_infra_failed"
    if exit_code == WORKER_EXIT_CANCELLED:
        return "worker_exit_cancelled"
    return "worker_exit_unknown"
