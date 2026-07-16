from __future__ import annotations

import unittest

from app.domain import Execution
from app.runtime.execution_lifecycle import (
    ALWAYS_ON_RUN_MODE,
    build_execution_lifecycle_metadata,
    resolve_persistent_cycle_policy,
)


class ExecutionLifecycleTests(unittest.TestCase):
    def test_persistent_cycle_implies_always_on_and_normalizes_policy(self) -> None:
        lifecycle = build_execution_lifecycle_metadata(
            trigger={"type": "manual"},
            workflow_metadata={
                "execution_lifecycle": {
                    "persistent_cycle": {
                        "enabled": True,
                        "interval_seconds": 15,
                        "jitter_ratio": 0.2,
                        "max_interval_seconds": 5,
                        "max_cycles": 10,
                        "max_no_progress_cycles": 3,
                    }
                }
            },
        )

        self.assertEqual(lifecycle["run_mode"], ALWAYS_ON_RUN_MODE)
        self.assertTrue(lifecycle["terminate_container_on_completion"])
        self.assertEqual(lifecycle["persistent_cycle"]["interval_seconds"], 15.0)
        self.assertEqual(lifecycle["persistent_cycle"]["jitter_ratio"], 0.2)
        self.assertEqual(lifecycle["persistent_cycle"]["max_interval_seconds"], 15.0)
        self.assertEqual(lifecycle["persistent_cycle"]["max_cycles"], 10)

    def test_always_on_without_explicit_cycle_keeps_finite_completion_semantics(self) -> None:
        lifecycle = build_execution_lifecycle_metadata(
            trigger={"run_mode": "always_on"},
            workflow_metadata={},
        )
        execution = Execution(
            workflow_id="workflow-1",
            runtime_adapter="native",
            metadata={"execution_lifecycle": lifecycle},
        )

        self.assertEqual(lifecycle["run_mode"], ALWAYS_ON_RUN_MODE)
        self.assertFalse(resolve_persistent_cycle_policy(execution).enabled)

    def test_invalid_optional_guards_remain_disabled(self) -> None:
        lifecycle = build_execution_lifecycle_metadata(
            trigger={},
            workflow_metadata={
                "execution_lifecycle": {
                    "persistent_cycle": {
                        "enabled": True,
                        "max_cycles": 0,
                        "max_no_progress_cycles": "invalid",
                    }
                }
            },
        )
        execution = Execution(
            workflow_id="workflow-1",
            runtime_adapter="native",
            metadata={"execution_lifecycle": lifecycle},
        )
        policy = resolve_persistent_cycle_policy(execution)

        self.assertIsNone(policy.max_cycles)
        self.assertIsNone(policy.max_no_progress_cycles)

    def test_cycle_policy_rejects_false_string_and_bounds_loop_controls(self) -> None:
        disabled = build_execution_lifecycle_metadata(
            trigger={},
            workflow_metadata={
                "execution_lifecycle": {"persistent_cycle": {"enabled": "false"}}
            },
        )
        bounded = build_execution_lifecycle_metadata(
            trigger={},
            workflow_metadata={
                "execution_lifecycle": {
                    "persistent_cycle": {
                        "enabled": "true",
                        "interval_seconds": 0.01,
                        "failure_backoff_multiplier": 0.5,
                        "history_limit": 1000,
                    }
                }
            },
        )

        self.assertNotIn("persistent_cycle", disabled)
        self.assertEqual(bounded["persistent_cycle"]["interval_seconds"], 1.0)
        self.assertEqual(bounded["persistent_cycle"]["failure_backoff_multiplier"], 1.0)
        self.assertEqual(bounded["persistent_cycle"]["history_limit"], 100)


if __name__ == "__main__":
    unittest.main()
