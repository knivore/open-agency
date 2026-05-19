from __future__ import annotations

import json
from typing import Any


def convert_str_to_dict(data: Any) -> dict[str, Any]:
    if data is None:
        return {}

    if isinstance(data, str):
        if "=" in data:
            try:
                return dict(pair.split("=") for pair in data.split(","))
            except ValueError as exc:
                raise ValueError("The string is not in a valid key=value format.") from exc
        try:
            result = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError("The string is not a valid JSON format.") from exc
        if isinstance(result, dict):
            return result
        raise ValueError("The JSON string does not contain a dictionary.")

    if isinstance(data, dict):
        return data

    raise TypeError("The provided data is neither a string nor a dictionary.")


def interpolate(template: Any, kwargs: dict[str, Any]) -> Any:
    if isinstance(template, str):
        return template.format(**kwargs)
    if isinstance(template, dict):
        return {key: interpolate(value, kwargs) for key, value in template.items()}
    if isinstance(template, list):
        return [interpolate(item, kwargs) for item in template]
    return template


__all__ = ["convert_str_to_dict", "interpolate"]
