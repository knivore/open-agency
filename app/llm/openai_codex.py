from __future__ import annotations

import asyncio
import httpx
import inspect
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from openai import OpenAI
from typing import Any, Dict, Iterator, List, Optional

from app.core.config import get_settings
from app.domain import ModelProfileDefinition
from app.llm.base import ModelMessage, ModelResponse, ModelToolCall
from app.llm.openai_helpers import sanitize_openai_message_name
from app.llm.registry import LLMEnvironmentConfig
from app.utils.oauth_pkce import OPENAI_CODEX_REDIRECT_URI, OAuthPKCEHandler

_refresh_locks: dict[str, asyncio.Lock] = {}
_refresh_locks_guard = threading.Lock()


def _refresh_lock_for(provider_id: str) -> asyncio.Lock:
    with _refresh_locks_guard:
        if provider_id not in _refresh_locks:
            _refresh_locks[provider_id] = asyncio.Lock()
        return _refresh_locks[provider_id]


class OpenAICodexModelClient:
    provider_key = "openai_codex"

    def __init__(self, profile: ModelProfileDefinition, env_config: LLMEnvironmentConfig):
        self.profile = profile
        self.env_config = env_config
        self.base_url = profile.base_url or "https://api.openai.com/v1"
        if self.base_url.rstrip("/") == "https://codex-api.openai.com/v1":
            self.base_url = "https://api.openai.com/v1"

        # Configuration can contain tokens (OAuth) or an API key
        self.config = profile.parameters or {}
        self.auth_mode = self.config.get("auth_mode", "chatgpt")
        self.oauth_profile_id = self.config.get("oauth_profile_id")
        self.skip_provider_hydration = bool(self.config.get("skip_provider_hydration"))

        # If auth_mode is 'api', we use standard API key from config or env
        self.api_key = self.config.get("api_key") or self.config.get("apiKey") or env_config.openai_api_key
        self.provider_id = self.config.get("provider_id") or self.config.get("providerId") or profile.provider_id

        # If auth_mode is 'chatgpt' (default), we use OAuth tokens
        self.access_token = self.config.get("access_token")
        self.refresh_token = self.config.get("refresh_token")
        self.expires_at = self.config.get("expires_at")
        self.client_id = self.config.get("client_id")
        self.redirect_uri = self.config.get("redirect_uri")
        self.account_id = self.config.get("account_id") or self.config.get("accountId")

        effective_api_key = self.api_key if self.auth_mode == "api" else self.access_token
        self.client = OpenAI(base_url=self.base_url, api_key=effective_api_key or "not-authorized")

    def _codex_cli_timeout_seconds(self, timeout_seconds: int | None = None) -> int:
        if timeout_seconds is not None:
            return timeout_seconds
        settings_timeout = get_settings().codex_cli_timeout_seconds or 1800
        env_timeout = os.getenv("CODEX_CLI_TIMEOUT_SECONDS") or None
        llm_timeout = os.getenv("LLM_REQUEST_TIMEOUT_SECONDS") or None
        raw_timeout = (
                env_timeout
                or self.config.get("codex_cli_timeout_seconds")
                or self.config.get("codexCliTimeoutSeconds")
                or self.config.get("request_timeout_seconds")
                or self.config.get("timeout_seconds")
                or llm_timeout
                or settings_timeout
        )
        try:
            timeout = int(float(raw_timeout))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Codex CLI timeout must be a number of seconds") from exc
        llm_timeout_value = None
        if llm_timeout is not None:
            try:
                llm_timeout_value = int(float(llm_timeout))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("LLM request timeout must be a number of seconds") from exc
        # Keep an explicit env override authoritative, but otherwise never allow the
        # Codex CLI fallback to exceed a stricter per-request LLM timeout.
        if env_timeout is None and llm_timeout_value is not None:
            timeout = min(timeout, llm_timeout_value)
        elif env_timeout is None:
            timeout = max(timeout, int(float(settings_timeout)))
        if timeout <= 0:
            raise RuntimeError("Codex CLI timeout must be greater than zero")
        return timeout

    async def _load_provider_oauth_state(self) -> None:
        if self.skip_provider_hydration:
            return
        repo = self.env_config.model_provider_repo
        if repo is None:
            return
        provider_result = repo.get(self.provider_id)
        if not inspect.isawaitable(provider_result):
            return
        provider = await provider_result
        if provider is None:
            return

        provider_config = dict(provider.config or {})
        profile_id = self.oauth_profile_id or provider_config.get("default_oauth_profile_id") or "default"
        self.oauth_profile_id = profile_id

        auth_profiles = provider_config.get("auth_profiles")
        oauth_config = {}
        if isinstance(auth_profiles, dict):
            record = auth_profiles.get(profile_id)
            if isinstance(record, dict):
                oauth_config = dict(record)
        if not oauth_config:
            oauth_config = provider_config

        self.auth_mode = oauth_config.get("auth_mode", provider_config.get("auth_mode", self.auth_mode))
        self.api_key = (
                oauth_config.get("api_key")
                or oauth_config.get("apiKey")
                or provider_config.get("api_key")
                or provider_config.get("apiKey")
                or self.api_key
                or self.env_config.openai_api_key
        )
        self.access_token = oauth_config.get("access_token", self.access_token)
        self.refresh_token = oauth_config.get("refresh_token", self.refresh_token)
        self.expires_at = oauth_config.get("expires_at", self.expires_at)
        self.client_id = oauth_config.get("client_id", provider_config.get("client_id", self.client_id))
        self.redirect_uri = oauth_config.get("redirect_uri", provider_config.get("redirect_uri", self.redirect_uri))
        self.account_id = oauth_config.get(
            "account_id",
            provider_config.get("account_id", self.account_id),
        )
        if self.api_key and self.base_url.rstrip("/") == "https://api.openai.com/v1":
            self.auth_mode = "api"
            self.client.api_key = self.api_key
        elif self.auth_mode == "chatgpt":
            self.client.api_key = self.access_token or "not-authorized"

    async def _ensure_authorized(self):
        """Step 4: The Request Wrapper - Ensure token is valid before request"""
        await self._load_provider_oauth_state()

        if self.auth_mode == "api":
            if not self.api_key:
                raise RuntimeError(
                    "OpenAI Codex API key not configured. Public API model calls require an OpenAI API key with model-request permissions."
                )
            self.client.api_key = self.api_key
            return

        if self.auth_mode == "chatgpt" and not self.access_token:
            return

        if not self.access_token:
            raise RuntimeError("OpenAI Codex not authorized. Please run OAuth flow (ChatGPT login) via the UI or API.")

        # Check if token is expired (with 5 min buffer)
        if self.expires_at and time.time() > (self.expires_at - 300):
            if self.refresh_token:
                async with _refresh_lock_for(self.profile.provider_id):
                    await self._load_provider_oauth_state()
                    if not (self.expires_at and time.time() > (self.expires_at - 300)):
                        return

                    client_id = self.client_id or "DEFAULT_CLIENT_ID"
                    redirect_uri = self.redirect_uri or OPENAI_CODEX_REDIRECT_URI
                    handler = OAuthPKCEHandler.for_provider(
                        "openai_codex",
                        client_id=client_id,
                        redirect_uri=redirect_uri,
                    )
                    new_tokens = await handler.refresh_token(self.refresh_token)
                    self.access_token = new_tokens["access_token"]
                    self.refresh_token = new_tokens.get("refresh_token", self.refresh_token)
                    self.expires_at = time.time() + new_tokens.get("expires_in", 3600)
                    self.account_id = OAuthPKCEHandler.extract_account_id(self.access_token) or self.account_id

                    # Update client
                    self.client.api_key = self.access_token

                    # Persist refreshed tokens to database
                    if self.env_config.model_provider_repo:
                        await self.env_config.model_provider_repo.update_tokens(
                            self.provider_id,
                            self.access_token,
                            self.refresh_token,
                            self.expires_at,
                            auth_profile_id=self.oauth_profile_id,
                            account_id=self.account_id,
                            auth_mode=self.auth_mode,
                            client_id=handler.client_id,
                            redirect_uri=handler.redirect_uri,
                        )
            else:
                raise RuntimeError(
                    "OpenAI Codex token expired. Please re-authorize via 'POST /model-providers/{id}/authorize'")

    def _messages_to_prompt(self, messages: List[ModelMessage]) -> str:
        rendered: list[str] = []
        for message in messages:
            role = message.role.upper()
            rendered.append(f"{role}:\n{message.content}")
            if message.tool_calls:
                # The CLI is stateless between native-runtime iterations, so preserve
                # the assistant tool request that each following TOOL message answers.
                rendered.append(
                    "ASSISTANT TOOL CALLS:\n"
                    + json.dumps(
                        [
                            {
                                "id": tool_call.id,
                                "name": tool_call.name,
                                "arguments": tool_call.arguments,
                            }
                            for tool_call in message.tool_calls
                        ],
                        ensure_ascii=True,
                    )
                )
            if message.tool_call_id:
                rendered.append(f"TOOL CALL ID: {message.tool_call_id}")
        return "\n\n".join(rendered).strip()

    def _cli_tool_contract(
            self,
            messages: List[ModelMessage],
            tools: List[Dict[str, Any]],
    ) -> tuple[List[ModelMessage], Dict[str, Any], set[str]]:
        tool_definitions: list[dict[str, Any]] = []
        allowed_tool_names: set[str] = set()
        for item in tools:
            function = item.get("function") if isinstance(item, dict) else None
            if not isinstance(function, dict):
                continue
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            allowed_tool_names.add(name)
            tool_definitions.append(
                {
                    "name": name,
                    "description": function.get("description") or "",
                    "parameters": function.get("parameters") or {"type": "object"},
                }
            )

        if not allowed_tool_names:
            return messages, {}, set()

        # Codex CLI cannot receive API-style function definitions. A strict final
        # envelope lets it select a tool while Agency retains policy and execution.
        response_schema: Dict[str, Any] = {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["tool_call", "final"]},
                "tool_name": {"type": "string", "enum": ["", *sorted(allowed_tool_names)]},
                "arguments_json": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["action", "tool_name", "arguments_json", "content"],
            "additionalProperties": False,
        }
        contract = {
            "available_agency_tools": tool_definitions,
            "response_contract": {
                "tool_call": {
                    "action": "tool_call",
                    "tool_name": "one exact available tool name",
                    "arguments_json": "a JSON object encoded as a string",
                    "content": "",
                },
                "final": {
                    "action": "final",
                    "tool_name": "",
                    "arguments_json": "{}",
                    "content": "the final answer",
                },
            },
        }
        return [
            *messages,
            ModelMessage(
                role="system",
                content=(
                    "You are the reasoning model inside Agency's native workflow runtime. "
                    "Do not execute tools, inspect installed connectors, search for MCP resources, "
                    "or use Codex built-in tools. Agency owns all tool execution and policy checks. "
                    "Return exactly one response-contract envelope. Select tool_call when an "
                    "available Agency tool is needed; after TOOL results are present, either select "
                    "another available tool or return final. Never claim a listed tool is unavailable.\n"
                    + json.dumps(contract, ensure_ascii=True)
                ),
            ),
        ], response_schema, allowed_tool_names

    def _parse_cli_tool_response(
            self,
            response: ModelResponse,
            *,
            allowed_tool_names: set[str],
    ) -> ModelResponse:
        payload = self._parse_structured_cli_content(response.content, schema_name="agency_native_tool_turn")
        if not isinstance(payload, dict):
            raise RuntimeError("Codex CLI Agency tool response must be a JSON object.")

        action = payload.get("action")
        if action == "final":
            response.content = str(payload.get("content") or "")
            return response
        if action != "tool_call":
            raise RuntimeError("Codex CLI Agency tool response has an invalid action.")

        tool_name = str(payload.get("tool_name") or "").strip()
        if tool_name not in allowed_tool_names:
            raise RuntimeError(f"Codex CLI requested unavailable Agency tool '{tool_name}'.")
        raw_arguments = payload.get("arguments_json") or "{}"
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Codex CLI returned invalid arguments for Agency tool '{tool_name}'.") from exc
        if not isinstance(arguments, dict):
            raise RuntimeError(f"Codex CLI arguments for Agency tool '{tool_name}' must be a JSON object.")

        response.content = None
        response.tool_calls = [
            ModelToolCall(id=None, name=tool_name, arguments=arguments, raw=payload)
        ]
        return response

    def _structured_cli_messages(
            self,
            messages: List[ModelMessage],
            *,
            schema_name: str,
            schema: Dict[str, Any],
    ) -> List[ModelMessage]:
        # ChatGPT OAuth mode uses Codex CLI rather than the public API, so structured
        # output is enforced by prompt contract and parsed locally after the CLI returns.
        schema_contract = json.dumps({"schema_name": schema_name, "schema": schema}, ensure_ascii=True)
        return [
            *messages,
            ModelMessage(
                role="system",
                content=(
                    "Return only JSON that validates against this JSON schema. "
                    "Do not include markdown fences, commentary, or extra keys.\n"
                    f"{schema_contract}"
                ),
            ),
        ]

    def _parse_structured_cli_content(self, content: Any, *, schema_name: str) -> Any:
        if not isinstance(content, str):
            return content
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start_candidates = [index for index in (text.find("{"), text.find("[")) if index >= 0]
            if not start_candidates:
                raise RuntimeError(f"Codex CLI structured response '{schema_name}' was not valid JSON.")
            start = min(start_candidates)
            end = max(text.rfind("}"), text.rfind("]"))
            if end <= start:
                raise RuntimeError(f"Codex CLI structured response '{schema_name}' was not valid JSON.")
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Codex CLI structured response '{schema_name}' was not valid JSON.") from exc

    def _generate_text_with_codex_cli(
            self,
            messages: List[ModelMessage],
            *,
            timeout_seconds: int | None = None,
            output_schema: Dict[str, Any] | None = None,
    ) -> ModelResponse:
        codex_binary = (
                self.config.get("codex_binary")
                or self.config.get("codexBinary")
                or os.getenv("CODEX_CLI_BINARY")
                or "codex"
        )
        executable = shutil.which(codex_binary)
        if executable is None:
            raise RuntimeError(
                "OpenAI Codex is configured for ChatGPT OAuth, which cannot call the public OpenAI API directly. "
                "Install Codex CLI in this backend environment, set CODEX_CLI_BINARY, run the backend on the host, "
                "or switch this model profile to API-key mode."
            )

        timeout = self._codex_cli_timeout_seconds(timeout_seconds)
        prompt = self._messages_to_prompt(messages)
        started_at = time.perf_counter()
        sandbox_mode = (
                self.config.get("codex_cli_sandbox")
                or self.config.get("codexCliSandbox")
                or os.getenv("CODEX_CLI_SANDBOX")
                or "read-only"
        )

        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".txt", delete=False) as output_file:
            output_path = output_file.name

        schema_path: str | None = None
        if output_schema:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as schema_file:
                json.dump(output_schema, schema_file, ensure_ascii=True)
                schema_path = schema_file.name

        command = [
            executable,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            str(sandbox_mode),
            "--skip-git-repo-check",
            "--model",
            self.profile.model,
            "--output-last-message",
            output_path,
        ]
        if schema_path:
            command.extend(["--output-schema", schema_path])
        command.append("-")
        cwd = self.config.get("codex_cwd") or os.getenv("CODEX_CLI_CWD") or os.getcwd()
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                text=True,
                capture_output=True,
                input=prompt,
                timeout=timeout,
                check=False,
            )
            content = ""
            try:
                with open(output_path, "r", encoding="utf-8") as handle:
                    content = handle.read().strip()
            except OSError:
                content = ""
            if completed.returncode != 0:
                stderr = (completed.stderr or completed.stdout or "").strip()
                raise RuntimeError(f"Codex CLI failed with exit code {completed.returncode}: {stderr}")
            if not content:
                content = (completed.stdout or "").strip()
            return ModelResponse(
                content=content,
                provider=self.profile.provider,
                model=self.profile.model,
                latency_ms=(time.perf_counter() - started_at) * 1000,
                raw_response={"command": command, "stderr": completed.stderr},
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Codex CLI timed out after {timeout} seconds.") from exc
        finally:
            try:
                os.unlink(output_path)
            except OSError:
                pass
            if schema_path:
                try:
                    os.unlink(schema_path)
                except OSError:
                    pass

    def _to_openai_messages(self, messages: List[ModelMessage]) -> List[Dict[str, Any]]:
        payload = []
        for message in messages:
            item: Dict[str, Any] = {
                "role": message.role,
                "content": message.content,
            }
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": sanitize_openai_message_name(tool_call.name) or tool_call.name,
                            "arguments": json.dumps(tool_call.arguments or {}),
                        },
                    }
                    for tool_call in message.tool_calls
                ]
            if message.name:
                sanitized_name = sanitize_openai_message_name(message.name)
                if sanitized_name:
                    item["name"] = sanitized_name
            if message.tool_call_id:
                item["tool_call_id"] = message.tool_call_id
            payload.append(item)
        return payload

    def _build_response(self, response: Any, started_at: float) -> ModelResponse:
        choice = response.choices[0].message if response.choices else None
        tool_calls: List[ModelToolCall] = []
        if choice and getattr(choice, "tool_calls", None):
            for tool_call in choice.tool_calls:
                arguments = tool_call.function.arguments
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {"raw": arguments}
                tool_calls.append(
                    ModelToolCall(
                        id=tool_call.id,
                        name=tool_call.function.name,
                        arguments=arguments,
                        raw=tool_call,
                    )
                )

        usage = {}
        if getattr(response, "usage", None):
            usage = response.usage.model_dump() if hasattr(response.usage, "model_dump") else dict(response.usage)

        content = choice.content if choice else None
        return ModelResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            raw_response=response,
            provider=self.profile.provider,
            model=self.profile.model,
            latency_ms=(time.perf_counter() - started_at) * 1000,
        )

    def _chat_options(
            self,
            *,
            temperature: Optional[float],
            max_tokens: Optional[int],
            stream: bool,
            **kwargs: Any,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.profile.model,
            "stream": stream,
            **kwargs,
        }
        resolved_temperature = temperature if temperature is not None else self.profile.temperature
        resolved_max_tokens = max_tokens if max_tokens is not None else self.profile.max_tokens
        if resolved_temperature is not None and not self.profile.model.startswith("gpt-5"):
            payload["temperature"] = resolved_temperature
        if resolved_max_tokens is not None:
            payload["max_tokens"] = resolved_max_tokens
        return payload

    def generate_text(
            self,
            messages: List[ModelMessage],
            *,
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> ModelResponse:
        asyncio.run(self._ensure_authorized())
        if self.auth_mode == "chatgpt":
            tools = kwargs.get("tools")
            if isinstance(tools, list) and tools:
                contract_messages, response_schema, allowed_tool_names = self._cli_tool_contract(messages, tools)
                if allowed_tool_names:
                    response = self._generate_text_with_codex_cli(
                        contract_messages,
                        output_schema=response_schema,
                    )
                    return self._parse_cli_tool_response(response, allowed_tool_names=allowed_tool_names)
            return self._generate_text_with_codex_cli(messages)

        started_at = time.perf_counter()
        response = self.client.chat.completions.create(
            messages=self._to_openai_messages(messages),
            **self._chat_options(
                temperature=temperature,
                max_tokens=max_tokens,
                stream=False,
                **kwargs,
            ),
        )
        return self._build_response(response, started_at)

    async def agenerate_text(
            self,
            messages: List[ModelMessage],
            *,
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> ModelResponse:
        await self._ensure_authorized()
        if self.auth_mode == "chatgpt":
            tools = kwargs.get("tools")
            if isinstance(tools, list) and tools:
                contract_messages, response_schema, allowed_tool_names = self._cli_tool_contract(messages, tools)
                if allowed_tool_names:
                    response = await asyncio.to_thread(
                        self._generate_text_with_codex_cli,
                        contract_messages,
                        output_schema=response_schema,
                    )
                    return self._parse_cli_tool_response(response, allowed_tool_names=allowed_tool_names)
            return await asyncio.to_thread(self._generate_text_with_codex_cli, messages)

        started_at = time.perf_counter()
        response = await asyncio.to_thread(
            self.client.chat.completions.create,
            messages=self._to_openai_messages(messages),
            **self._chat_options(
                temperature=temperature,
                max_tokens=max_tokens,
                stream=False,
                **kwargs,
            ),
        )
        return self._build_response(response, started_at)

    def generate_structured(
            self,
            messages: List[ModelMessage],
            *,
            schema: Dict[str, Any],
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> ModelResponse:
        asyncio.run(self._ensure_authorized())
        schema_name = kwargs.pop("schema_name", "structured_output")
        if self.auth_mode == "chatgpt":
            response = self._generate_text_with_codex_cli(
                self._structured_cli_messages(messages, schema_name=schema_name, schema=schema)
            )
            response.content = self._parse_structured_cli_content(response.content, schema_name=schema_name)
            return response

        started_at = time.perf_counter()
        response = self.client.chat.completions.create(
            messages=self._to_openai_messages(messages),
            **self._chat_options(
                temperature=temperature,
                max_tokens=max_tokens,
                stream=False,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "schema": schema,
                    },
                },
                **kwargs,
            ),
        )
        model_response = self._build_response(response, started_at)
        if isinstance(model_response.content, str):
            try:
                model_response.content = json.loads(model_response.content)
            except json.JSONDecodeError:
                pass
        return model_response

    async def agenerate_structured(
            self,
            messages: List[ModelMessage],
            *,
            schema: Dict[str, Any],
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> ModelResponse:
        await self._ensure_authorized()
        schema_name = kwargs.pop("schema_name", "structured_output")
        if self.auth_mode == "chatgpt":
            response = await asyncio.to_thread(
                self._generate_text_with_codex_cli,
                self._structured_cli_messages(messages, schema_name=schema_name, schema=schema),
            )
            response.content = self._parse_structured_cli_content(response.content, schema_name=schema_name)
            return response

        started_at = time.perf_counter()
        response = await asyncio.to_thread(
            self.client.chat.completions.create,
            messages=self._to_openai_messages(messages),
            **self._chat_options(
                temperature=temperature,
                max_tokens=max_tokens,
                stream=False,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "schema": schema,
                    },
                },
                **kwargs,
            ),
        )
        model_response = self._build_response(response, started_at)
        if isinstance(model_response.content, str):
            try:
                model_response.content = json.loads(model_response.content)
            except json.JSONDecodeError:
                pass
        return model_response

    def stream_text(
            self,
            messages: List[ModelMessage],
            *,
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> Iterator[str]:
        asyncio.run(self._ensure_authorized())

        stream = self.client.chat.completions.create(
            messages=self._to_openai_messages(messages),
            **self._chat_options(
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                **kwargs,
            ),
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            if content:
                yield content

    def count_tokens(self, messages: List[ModelMessage], **kwargs: Any) -> Optional[int]:
        return None

    def _auth_endpoint(self, action: str) -> str:
        if action == "device_authorize":
            return f"/model-providers/{self.provider_id}/device-authorize"
        if action == "authorize":
            return f"/model-providers/{self.provider_id}/authorize"
        return f"/model-providers/{self.provider_id}"

    def _classify_auth_error(self, error_text: str, *, status_code: int | None = None) -> str | None:
        lowered = error_text.lower()
        if "model.request" in lowered or "missing scope" in lowered or "missing scopes" in lowered:
            return "missing_model_request_scope"
        if "token expired" in lowered or ("expired" in lowered and "token" in lowered):
            return "token_expired"
        if (
                status_code in {401, 403}
                or "unauthorized" in lowered
                or "not authorized" in lowered
                or "invalid api key" in lowered
                or "invalid_api_key" in lowered
                or "invalid token" in lowered
        ):
            return "invalid_credentials"
        return None

    def _auth_failure_payload(
            self,
            *,
            error: str,
            error_code: str,
            status_code: int | None = None,
            raw_error: Any | None = None,
    ) -> Dict[str, Any]:
        oauth_mode = self.auth_mode == "chatgpt"
        action = "device_authorize" if oauth_mode else "update_api_key"
        auth_status = "reauthorization_required" if oauth_mode else "invalid_credentials"
        if error_code == "missing_model_request_scope":
            auth_status = "missing_scope"
        return {
            "ok": False,
            "provider": self.profile.provider,
            "base_url": self.base_url,
            "status_code": status_code,
            "error": error,
            "error_code": error_code,
            "auth_status": auth_status,
            "auth_required": True,
            "reauthorization_required": oauth_mode,
            "auth_mode": self.auth_mode,
            "auth_action": action,
            "auth_endpoint": self._auth_endpoint(action),
            "auth_profile_id": self.oauth_profile_id,
            "provider_id": self.provider_id,
            "raw_error": raw_error,
        }

    def _chatgpt_health_payload(self) -> Dict[str, Any]:
        codex_binary = (
                self.config.get("codex_binary")
                or self.config.get("codexBinary")
                or os.getenv("CODEX_CLI_BINARY")
                or "codex"
        )
        executable = shutil.which(codex_binary)
        return {
            "ok": executable is not None,
            "provider": self.profile.provider,
            "base_url": self.base_url,
            "status_code": None,
            "auth_status": "ok" if executable is not None else "codex_cli_unavailable",
            "auth_required": executable is None,
            "reauthorization_required": False,
            "auth_mode": self.auth_mode,
            "auth_action": None if executable is not None else "install_codex_cli",
            "auth_endpoint": None,
            "auth_profile_id": self.oauth_profile_id,
            "provider_id": self.provider_id,
            "codex_cli_available": executable is not None,
            "codex_cli_binary": codex_binary,
        }

    async def ahealth_check(self) -> Dict[str, Any]:
        try:
            await self._ensure_authorized()
        except Exception as exc:
            error = str(exc)
            error_code = self._classify_auth_error(error) or "authorization_failed"
            return self._auth_failure_payload(error=error, error_code=error_code)

        if self.auth_mode == "chatgpt":
            return self._chatgpt_health_payload()

        return await asyncio.to_thread(self.health_check)

    def health_check(self) -> Dict[str, Any]:
        try:
            asyncio.run(self._ensure_authorized())
        except Exception as exc:
            error = str(exc)
            error_code = self._classify_auth_error(error) or "authorization_failed"
            return self._auth_failure_payload(error=error, error_code=error_code)

        if self.auth_mode == "chatgpt":
            return self._chatgpt_health_payload()

        try:
            url = self.base_url.rstrip("/") + "/models"
            bearer_token = self.api_key if self.auth_mode == "api" else self.access_token
            with httpx.Client(timeout=5.0) as client:
                response = client.get(url, headers={"Authorization": f"Bearer {bearer_token}"})
            raw_error = None
            error_text = response.text
            try:
                payload = response.json()
                raw_error = payload.get("error") if isinstance(payload, dict) else payload
                if isinstance(raw_error, dict):
                    error_text = str(raw_error.get("message") or error_text)
            except Exception:
                pass
            if 200 <= response.status_code < 300:
                return {
                    "ok": True,
                    "provider": self.profile.provider,
                    "base_url": self.base_url,
                    "status_code": response.status_code,
                    "auth_status": "ok",
                    "auth_required": False,
                    "reauthorization_required": False,
                    "auth_mode": self.auth_mode,
                    "auth_profile_id": self.oauth_profile_id,
                    "provider_id": self.provider_id,
                }
            error_code = self._classify_auth_error(error_text, status_code=response.status_code)
            if error_code is not None:
                return self._auth_failure_payload(
                    error=error_text,
                    error_code=error_code,
                    status_code=response.status_code,
                    raw_error=raw_error,
                )
            return {
                "ok": False,
                "provider": self.profile.provider,
                "base_url": self.base_url,
                "status_code": response.status_code,
                "error": error_text,
            }
        except Exception as exc:
            return {"ok": False, "provider": self.profile.provider, "base_url": self.base_url, "error": str(exc)}
