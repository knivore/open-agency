from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from app.domain import ToolDefinition, ToolType
from app.runtime.native.errors import ToolExecutionError
from app.tools.policies.command import blocked_command_reason
from .base import ToolExecutionContext


class ShellCommandToolExecutor:
    tool_type = ToolType.SHELL_COMMAND
    _DEFAULT_TIMEOUT_SECONDS = 30
    _DEFAULT_MAX_TIMEOUT_SECONDS = 7_200
    _DEFAULT_MAX_OUTPUT_BYTES = 50_000
    _DEFAULT_MAX_OUTPUT_LINES = 200
    _CONTROL_CHAR_RATIO_THRESHOLD = 0.10

    def execute(self, tool: ToolDefinition, arguments: dict[str, object], context: ToolExecutionContext) -> dict[
        str, object]:
        if not tool.security.allow_shell:
            raise ToolExecutionError(f"Shell tool '{tool.id}' is disabled")
        if not tool.security.sandbox_required:
            raise ToolExecutionError(f"Shell tool '{tool.id}' requires sandbox_required=True")
        if not tool.security.requires_approval:
            raise ToolExecutionError(f"Shell tool '{tool.id}' requires requires_approval=True")

        command = arguments.get("command") or tool.implementation.config.get("command") or tool.implementation.target
        if not str(command).strip():
            raise ToolExecutionError("Shell command tools require a non-empty command")
        self._enforce_command_policy(str(command))

        mode = str(arguments.get("mode") or tool.implementation.config.get("mode") or "auto").lower()
        executable, shell_label = self._resolve_shell(mode)
        timeout = self._resolve_timeout(arguments, tool)
        cwd = arguments.get("cwd") or tool.implementation.config.get("cwd")
        started_at = time.perf_counter()
        try:
            completed = subprocess.run(  # noqa: S603
                self._build_shell_invocation(executable, shell_label, str(command)),
                shell=False,
                capture_output=True,
                cwd=str(cwd) if cwd else None,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ToolExecutionError(
                f"Shell mode '{mode}' is not available. Use mode='auto', 'bash', 'sh', 'powershell', or 'cmd'."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            stderr = self._decode_bytes(exc.stderr or b"")
            stdout = self._decode_bytes(exc.stdout or b"")
            presentation = self._format_presentation(
                stdout=stdout,
                stderr=stderr or f"Command timed out after {timeout}s",
                returncode=124,
                duration_ms=duration_ms,
                context=context,
            )
            return {
                "status": "error",
                "command": str(command),
                "mode": mode,
                "shell": shell_label,
                "stdout": self._truncate_for_return(stdout),
                "stderr": self._truncate_for_return(stderr or f"Command timed out after {timeout}s"),
                "returncode": 124,
                "exit_code": 124,
                "duration_ms": duration_ms,
                "output_text": presentation["text"],
                "truncated": presentation["truncated"],
                "overflow_path": presentation["overflow_path"],
            }

        duration_ms = int((time.perf_counter() - started_at) * 1000)
        stdout_is_binary = self._is_binary(completed.stdout)
        stderr_is_binary = self._is_binary(completed.stderr)
        stdout = "" if stdout_is_binary else self._decode_bytes(completed.stdout)
        stderr = "" if stderr_is_binary else self._decode_bytes(completed.stderr)
        binary_notes = []
        if stdout_is_binary:
            binary_notes.append(
                "[error] command produced binary stdout. Redirect it to a file and inspect it with an appropriate viewer."
            )
        if stderr_is_binary:
            binary_notes.append(
                "[error] command produced binary stderr. Redirect it to a file and inspect it with an appropriate viewer."
            )
        if binary_notes:
            stdout = "\n".join(binary_notes)

        presentation = self._format_presentation(
            stdout=stdout,
            stderr=stderr,
            returncode=completed.returncode,
            duration_ms=duration_ms,
            context=context,
        )
        return {
            "status": "ok" if completed.returncode == 0 else "error",
            "command": str(command),
            "mode": mode,
            "shell": shell_label,
            "stdout": self._truncate_for_return(stdout),
            "stderr": self._truncate_for_return(stderr),
            "returncode": completed.returncode,
            "exit_code": completed.returncode,
            "duration_ms": duration_ms,
            "output_text": presentation["text"],
            "truncated": presentation["truncated"],
            "overflow_path": presentation["overflow_path"],
        }

    def _resolve_shell(self, mode: str) -> tuple[str | None, str]:
        normalized = mode.lower().strip()
        if normalized in {"", "auto", "shell", "cli"}:
            if os.name == "nt":
                return None, "cmd"
            bash = shutil.which("bash")
            if bash:
                return bash, "bash"
            return shutil.which("sh") or "/bin/sh", "sh"
        if normalized in {"bash", "linux", "mac", "macos"}:
            executable = shutil.which("bash")
            if executable is None:
                raise FileNotFoundError("bash")
            return executable, "bash"
        if normalized in {"sh", "posix"}:
            executable = shutil.which("sh") or "/bin/sh"
            return executable, "sh"
        if normalized in {"zsh"}:
            executable = shutil.which("zsh")
            if executable is None:
                raise FileNotFoundError("zsh")
            return executable, "zsh"
        if normalized in {"pwsh", "powershell", "windows"}:
            executable = shutil.which("pwsh") or shutil.which("powershell")
            if executable is None:
                raise FileNotFoundError("powershell")
            return executable, Path(executable).name
        if normalized == "cmd":
            if os.name == "nt":
                return None, "cmd"
            executable = shutil.which("cmd")
            if executable is None:
                raise FileNotFoundError("cmd")
            return executable, "cmd"
        raise ToolExecutionError(
            f"Unknown shell mode '{mode}'. Use one of: auto, bash, sh, zsh, powershell, pwsh, cmd."
        )

    def _build_shell_invocation(self, executable: str | None, shell_label: str, command: str) -> list[str]:
        normalized = shell_label.lower()
        if normalized in {"bash", "sh", "zsh"}:
            return [executable or normalized, "-lc", command]
        if normalized in {"pwsh", "powershell"}:
            return [executable or normalized, "-NoLogo", "-NonInteractive", "-Command", command]
        if normalized == "cmd":
            return [executable or os.environ.get("COMSPEC", "cmd"), "/d", "/s", "/c", command]
        raise ToolExecutionError(f"Shell mode '{shell_label}' is not supported")

    def _enforce_command_policy(self, command: str) -> None:
        reason = blocked_command_reason(command)
        if reason:
            raise ToolExecutionError(f"Blocked command: {reason}")

    def _resolve_timeout(self, arguments: dict[str, object], tool: ToolDefinition) -> int:
        default_timeout = int(tool.implementation.config.get("timeout", self._DEFAULT_TIMEOUT_SECONDS))
        max_timeout = int(tool.implementation.config.get("max_timeout", self._DEFAULT_MAX_TIMEOUT_SECONDS))
        requested = arguments.get("timeout_seconds")
        if requested is None:
            return default_timeout
        try:
            timeout = int(requested)
        except (TypeError, ValueError) as exc:
            raise ToolExecutionError("timeout_seconds must be an integer") from exc
        if timeout <= 0:
            raise ToolExecutionError("timeout_seconds must be greater than zero")
        if timeout > max_timeout:
            raise ToolExecutionError(f"timeout_seconds cannot exceed {max_timeout}")
        return timeout

    def _format_presentation(
            self,
            *,
            stdout: str,
            stderr: str,
            returncode: int,
            duration_ms: int,
            context: ToolExecutionContext,
    ) -> dict[str, object]:
        output_parts = [stdout.rstrip("\n")] if stdout else []
        if stderr:
            output_parts.append(f"[stderr]\n{stderr.rstrip()}")
        output = "\n".join(part for part in output_parts if part)
        if not output:
            output = "(no output)"

        max_bytes = self._DEFAULT_MAX_OUTPUT_BYTES
        max_lines = self._DEFAULT_MAX_OUTPUT_LINES
        lines = output.splitlines()
        encoded = output.encode("utf-8", errors="replace")
        truncated = len(lines) > max_lines or len(encoded) > max_bytes
        overflow_path: str | None = None

        if truncated:
            overflow_dir = Path(tempfile.gettempdir()) / "agency-command-output"
            overflow_dir.mkdir(parents=True, exist_ok=True)
            overflow_path = str(overflow_dir / f"{context.execution_id}-{uuid4().hex}.txt")
            Path(overflow_path).write_text(output, encoding="utf-8")
            line_limited = "\n".join(lines[:max_lines])
            output = self._truncate_bytes(line_limited, max_bytes)
            output = (
                f"{output}\n"
                f"--- output truncated ({len(lines)} lines, {len(encoded)} bytes) ---\n"
                f"Full output: {overflow_path}\n"
                f"Explore: run_command command=\"cat {overflow_path} | grep <pattern>\"\n"
                f"         run_command command=\"tail -100 {overflow_path}\""
            )

        return {
            "text": f"{output}\n[exit:{returncode} | {duration_ms}ms]",
            "truncated": truncated,
            "overflow_path": overflow_path,
        }

    def _is_binary(self, payload: bytes | None) -> bool:
        if not payload:
            return False
        if b"\x00" in payload:
            return True
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            return True
        if not text:
            return False
        control_chars = sum(1 for char in text if ord(char) < 32 and char not in "\n\r\t")
        return control_chars / max(len(text), 1) > self._CONTROL_CHAR_RATIO_THRESHOLD

    def _decode_bytes(self, payload: bytes | None) -> str:
        if not payload:
            return ""
        return payload.decode("utf-8", errors="replace")

    def _truncate_bytes(self, value: str, max_bytes: int) -> str:
        encoded = value.encode("utf-8", errors="replace")
        if len(encoded) <= max_bytes:
            return value
        return encoded[:max_bytes].decode("utf-8", errors="ignore")

    def _truncate_for_return(self, value: str) -> str:
        if not value:
            return ""
        lines = value.splitlines()
        output = "\n".join(lines[: self._DEFAULT_MAX_OUTPUT_LINES])
        return self._truncate_bytes(output, self._DEFAULT_MAX_OUTPUT_BYTES)
