from __future__ import annotations

from typing import Any


def record_external_example_audit(*args: Any, **kwargs: Any) -> None:
    return None


def external_example_observability_manifest() -> dict[str, object]:
    return {
        "module": "external_example_pack",
        "hook_refs": {
            "audit": "tests.fixtures.external_module_pack.observability:record_external_example_audit",
        },
    }

