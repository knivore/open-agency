from __future__ import annotations

from typing import Any


def append_external_example_delta(*args: Any, **kwargs: Any) -> None:
    return None


def external_example_neo4j_handlers() -> dict[str, Any]:
    return {}


def external_example_graph_manifest() -> dict[str, object]:
    return {
        "module": "external_example_pack",
        "delta_builder_refs": (
            "tests.fixtures.external_module_pack.graph:append_external_example_delta",
        ),
        "neo4j_handler_map_refs": (
            "tests.fixtures.external_module_pack.graph:external_example_neo4j_handlers",
        ),
    }

