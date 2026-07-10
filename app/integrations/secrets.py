from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SecretResolutionResult:
    value: str | None
    source: str
    identifier: str
    error: str | None = None


def is_onecli_secret_ref(secret_ref: str | None) -> bool:
    return isinstance(secret_ref, str) and secret_ref.strip().lower().startswith("onecli://")


def onecli_secret_identifier(secret_ref: str) -> str:
    return secret_ref.strip()[len("onecli://"):]


def resolve_secret_ref(secret_ref: str) -> SecretResolutionResult:
    if secret_ref.startswith("env://"):
        env_var = secret_ref[len("env://"):].strip()
        if not env_var:
            return SecretResolutionResult(value=None, source="env", identifier=secret_ref,
                                          error="Environment secret ref is empty.")
        value = os.getenv(env_var)
        if value is None:
            return SecretResolutionResult(
                value=None,
                source="env",
                identifier=env_var,
                error=f"Environment variable '{env_var}' is not set.",
            )
        return SecretResolutionResult(value=value, source="env", identifier=env_var)

    if secret_ref.startswith("env:"):
        env_var = secret_ref[len("env:"):].strip()
        if not env_var:
            return SecretResolutionResult(value=None, source="env", identifier=secret_ref,
                                          error="Environment secret ref is empty.")
        value = os.getenv(env_var)
        if value is None:
            return SecretResolutionResult(
                value=None,
                source="env",
                identifier=env_var,
                error=f"Environment variable '{env_var}' is not set.",
            )
        return SecretResolutionResult(value=value, source="env", identifier=env_var)

    return SecretResolutionResult(
        value=None,
        source="unresolved",
        identifier=secret_ref,
        error="Secret ref is not environment-resolvable yet. Supported formats: env://VAR_NAME or env:VAR_NAME.",
    )
