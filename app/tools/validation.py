from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain import ToolDefinition, ToolType


@dataclass
class ToolValidationIssue:
    code: str
    message: str
    severity: str = "error"


@dataclass
class ToolValidationResult:
    valid: bool
    validation_errors: list[dict[str, Any]] = field(default_factory=list)
    validation_warnings: list[dict[str, Any]] = field(default_factory=list)


class ToolValidationService:
    def validate(self, tool: ToolDefinition) -> ToolValidationResult:
        errors: list[ToolValidationIssue] = []
        warnings: list[ToolValidationIssue] = []

        if not tool.input_schema:
            errors.append(
                ToolValidationIssue(code="tool.input_schema.missing", message="Tool input_schema is required"))

        direct_secret_keys = {"api_key", "apikey", "token", "password", "secret"}
        lowered_config = {str(key).lower() for key in tool.implementation.config}
        if lowered_config & direct_secret_keys:
            errors.append(
                ToolValidationIssue(
                    code="tool.credentials.embedded",
                    message="Secrets must be stored via CredentialReference, not embedded in ToolDefinition",
                )
            )

        if tool.tool_type == ToolType.HTTP_REQUEST and not tool.security.allowlisted_domains:
            errors.append(ToolValidationIssue(code="tool.http.allowlist.missing",
                                              message="HTTP tools require allowlisted domains"))

        if tool.tool_type == ToolType.A2A_REMOTE_AGENT:
            if not tool.security.allowlisted_domains:
                errors.append(ToolValidationIssue(code="tool.a2a.allowlist.missing",
                                                  message="A2A remote agent tools require allowlisted domains"))
            if tool.implementation.config.get("stub_response") is None and not tool.implementation.target.startswith(
                    ("http://", "https://")):
                errors.append(ToolValidationIssue(code="tool.a2a.target.invalid",
                                                  message="A2A remote agent tools require an HTTP endpoint target or stub_response"))

        if tool.tool_type == ToolType.SQL_QUERY and not tool.security.read_only_sql:
            warnings.append(ToolValidationIssue(code="tool.sql.read_only.disabled", message="SQL tool is not read-only",
                                                severity="warning"))

        if tool.tool_type == ToolType.PYTHON_FUNCTION:
            if not (
                    tool.security.module_allowlist
                    or tool.implementation.target.startswith("app.tools.implementations.")
                    or tool.implementation.target == "tests.native_test_tools"
            ):
                errors.append(ToolValidationIssue(code="tool.python.module_allowlist.missing",
                                                  message="Python tools require a module allowlist"))
            if not (
                    tool.security.function_allowlist or tool.implementation.callable_name or tool.implementation.entrypoint):
                warnings.append(ToolValidationIssue(code="tool.python.function_allowlist.missing",
                                                    message="Python tool should declare a callable allowlist",
                                                    severity="warning"))

        if tool.tool_type == ToolType.SHELL_COMMAND:
            if not tool.security.allow_shell:
                errors.append(ToolValidationIssue(code="tool.shell.disabled",
                                                  message="Shell command tools must opt in with allow_shell=True"))
            if not tool.security.requires_approval:
                errors.append(ToolValidationIssue(code="tool.shell.approval.missing",
                                                  message="Shell command tools require approval"))
            if not tool.security.sandbox_required:
                errors.append(ToolValidationIssue(code="tool.shell.sandbox.missing",
                                                  message="Shell command tools require sandboxing"))

        valid = not errors
        return ToolValidationResult(
            valid=valid,
            validation_errors=[issue.__dict__ for issue in errors],
            validation_warnings=[issue.__dict__ for issue in warnings],
        )
