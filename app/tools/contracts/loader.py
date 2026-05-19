from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .models import ToolContract

DEFAULT_CONTRACT_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


class ToolContractLoadError(RuntimeError):
    pass


def load_contracts(schema_dir: Path | None = None) -> list[ToolContract]:
    root = schema_dir or DEFAULT_CONTRACT_SCHEMA_DIR
    if not root.exists():
        return []
    return [_load_contract(path) for path in _contract_paths(root)]


def _contract_paths(root: Path) -> Iterable[Path]:
    return sorted(root.glob("*.contract.json"))


def _load_contract(path: Path) -> ToolContract:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ToolContractLoadError(f"Contract file is invalid JSON: {path}") from exc
    try:
        contract = ToolContract.model_validate(payload)
    except Exception as exc:
        raise ToolContractLoadError(f"Contract file is invalid: {path}: {exc}") from exc
    _validate_basic_contract(contract, path)
    return contract


def _validate_basic_contract(contract: ToolContract, path: Path) -> None:
    if not contract.name.strip():
        raise ToolContractLoadError(f"Contract is missing name: {path}")
    if not contract.version.strip():
        raise ToolContractLoadError(f"Contract '{contract.name}' is missing version: {path}")
    for field_name in ("inputs", "outputs"):
        schema = getattr(contract, field_name)
        if schema.get("type") != "object":
            raise ToolContractLoadError(
                f"Contract '{contract.name}' {field_name} schema must be an object schema: {path}"
            )
