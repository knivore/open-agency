from __future__ import annotations

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .models import ToolContract


class ToolContractValidationError(ValueError):
    pass


def validate_tool_input(contract: ToolContract, payload: dict) -> None:
    _validate_schema(contract.inputs, payload, f"{contract.name} input")


def validate_tool_output(contract: ToolContract, payload: dict) -> None:
    _validate_schema(contract.outputs, payload, f"{contract.name} output")


def _validate_schema(schema: dict, payload: dict, label: str) -> None:
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    if errors:
        raise ToolContractValidationError(_format_error(errors[0], label))


def _format_error(error: ValidationError, label: str) -> str:
    path = ".".join(str(part) for part in error.path) or "<root>"
    return f"{label} validation failed at {path}: {error.message}"
