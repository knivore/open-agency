from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.tools.contracts.models import PolicyVerdict
from .browser import evaluate_browser_policy
from .command import evaluate_command_run_policy
from .files import evaluate_file_write_text_policy, evaluate_markdown_to_word_policy, evaluate_spreadsheet_write_policy
from .http import evaluate_http_request_policy
from .rules import evaluate_sandbox_edit_policy


class PolicyEngine:
    def __init__(
            self,
            allowed_repos: list[str] | None = None,
            allowed_file_write_dirs: list[str] | None = None,
            allowed_http_hosts: list[str] | None = None,
    ):
        settings = get_settings()
        self.allowed_repos = allowed_repos if allowed_repos is not None else settings.parsed_sandbox_edit_allowed_repos
        self.allowed_file_write_dirs = (
            allowed_file_write_dirs
            if allowed_file_write_dirs is not None
            else settings.parsed_tool_file_write_allowed_dirs
        )
        self.allowed_http_hosts = allowed_http_hosts if allowed_http_hosts is not None else settings.parsed_tool_http_allowed_hosts

    def evaluate(self, tool_name: str, payload: dict[str, Any], *, actor: str | None = None) -> PolicyVerdict:
        if tool_name == "sandbox-edit":
            return evaluate_sandbox_edit_policy(payload, allowed_repos=self.allowed_repos, actor=actor)
        if tool_name == "agency.http.request":
            return evaluate_http_request_policy(payload, allowed_hosts=self.allowed_http_hosts, actor=actor)
        if tool_name == "agency.command.run":
            return evaluate_command_run_policy(payload, actor=actor)
        if tool_name == "agency.file.write-text":
            return evaluate_file_write_text_policy(payload, allowed_dirs=self.allowed_file_write_dirs, actor=actor)
        if tool_name == "agency.document.markdown-to-word":
            return evaluate_markdown_to_word_policy(payload, actor=actor)
        if tool_name == "agency.excel.write-text":
            return evaluate_spreadsheet_write_policy(
                payload,
                allowed_dirs=self.allowed_file_write_dirs,
                source_path_keys=["text_file_path"],
                actor=actor,
            )
        if tool_name == "agency.excel.write-json":
            return evaluate_spreadsheet_write_policy(
                payload,
                allowed_dirs=self.allowed_file_write_dirs,
                source_path_keys=["json_file_path"],
                actor=actor,
            )
        if tool_name == "agency.excel.write-image":
            return evaluate_spreadsheet_write_policy(
                payload,
                allowed_dirs=self.allowed_file_write_dirs,
                source_path_keys=["image_path"],
                actor=actor,
            )
        if tool_name.startswith("agency.browser."):
            return evaluate_browser_policy(
                tool_name,
                payload,
                allowed_hosts=self.allowed_http_hosts,
                actor=actor,
            )
        return PolicyVerdict()
