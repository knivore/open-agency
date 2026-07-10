from __future__ import annotations

from pydantic import BaseModel, Field


class ExternalExampleCommand(BaseModel):
    """Tiny domain contract owned by the example external pack."""

    target_id: str = Field(min_length=1)
    action: str = Field(min_length=1)


def external_example_domain_manifest() -> dict[str, object]:
    return {
        "module": "external_example_pack",
        "model_refs": (
            "tests.fixtures.external_module_pack.domain:ExternalExampleCommand",
        ),
    }

