from __future__ import annotations

import httpx
import json
import time
from openai import AzureOpenAI
from typing import Any, Dict, Iterator, List, Optional

from app.domain import ModelProfileDefinition
from app.llm.base import ModelMessage, ModelResponse, ModelToolCall
from app.llm.registry import LLMEnvironmentConfig


class AzureOpenAIModelClient:
    provider_key = "azure_openai"

    def __init__(self, profile: ModelProfileDefinition, env_config: LLMEnvironmentConfig):
        self.profile = profile
        self.env_config = env_config
        self.base_url = profile.base_url

        # Support for OAuth tokens
        self.config = profile.parameters or {}
        self.access_token = self.config.get("access_token")
        self.refresh_token = self.config.get("refresh_token")
        self.expires_at = self.config.get("expires_at")

        self.api_key = profile.api_key_ref or env_config.openai_api_key  # Defaulting to some key if no token
        self.api_version = self.config.get("api_version") or "2024-02-01"

        if self.access_token:
            self.client = AzureOpenAI(
                azure_endpoint=self.base_url,
                azure_ad_token=self.access_token,
                api_version=self.api_version
            )
        else:
            self.client = AzureOpenAI(
                azure_endpoint=self.base_url,
                api_key=self.api_key,
                api_version=self.api_version
            )

    async def _ensure_authorized(self):
        """Ensure token is valid if using OAuth flow"""
        if self.access_token:
            # Check if token is expired (with 5 min buffer)
            if self.expires_at and time.time() > (self.expires_at - 300):
                if self.refresh_token:
                    from app.utils.oauth_pkce import OAuthPKCEHandler
                    client_id = self.config.get("client_id") or "DEFAULT_CLIENT_ID"
                    tenant_id = self.config.get("tenant_id")
                    handler = OAuthPKCEHandler.for_provider("azure_openai", client_id=client_id, redirect_uri="",
                                                            tenant_id=tenant_id)
                    new_tokens = await handler.refresh_token(self.refresh_token)
                    self.access_token = new_tokens["access_token"]
                    self.refresh_token = new_tokens.get("refresh_token", self.refresh_token)
                    self.expires_at = time.time() + new_tokens.get("expires_in", 3600)

                    # Update client
                    self.client = AzureOpenAI(
                        azure_endpoint=self.base_url,
                        azure_ad_token=self.access_token,
                        api_version=self.api_version
                    )

                    # Persist refreshed tokens to database
                    if self.env_config.model_provider_repo:
                        await self.env_config.model_provider_repo.update_tokens(
                            self.profile.provider_id,
                            self.access_token,
                            self.refresh_token,
                            self.expires_at
                        )
                else:
                    raise RuntimeError("Azure OAuth token expired. Please re-authorize via 'POST /model-providers/{id}/authorize'")

    def _to_openai_messages(self, messages: List[ModelMessage]) -> List[Dict[str, Any]]:
        payload = []
        for message in messages:
            item: Dict[str, Any] = {
                "role": message.role,
                "content": message.content,
            }
            if message.name:
                item["name"] = message.name
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

    def generate_text(
            self,
            messages: List[ModelMessage],
            *,
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> ModelResponse:
        import asyncio
        asyncio.run(self._ensure_authorized())
        started_at = time.perf_counter()
        response = self.client.chat.completions.create(
            model=self.profile.model,
            messages=self._to_openai_messages(messages),
            temperature=temperature if temperature is not None else self.profile.temperature,
            max_tokens=max_tokens if max_tokens is not None else self.profile.max_tokens,
            stream=False,
            **kwargs,
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
        import asyncio
        asyncio.run(self._ensure_authorized())
        started_at = time.perf_counter()
        response = self.client.chat.completions.create(
            model=self.profile.model,
            messages=self._to_openai_messages(messages),
            temperature=temperature if temperature is not None else self.profile.temperature,
            max_tokens=max_tokens if max_tokens is not None else self.profile.max_tokens,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": kwargs.pop("schema_name", "structured_output"),
                    "schema": schema,
                },
            },
            stream=False,
            **kwargs,
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
        import asyncio
        asyncio.run(self._ensure_authorized())
        stream = self.client.chat.completions.create(
            model=self.profile.model,
            messages=self._to_openai_messages(messages),
            temperature=temperature if temperature is not None else self.profile.temperature,
            max_tokens=max_tokens if max_tokens is not None else self.profile.max_tokens,
            stream=True,
            **kwargs,
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

    def health_check(self) -> Dict[str, Any]:
        try:
            import asyncio
            asyncio.run(self._ensure_authorized())
            # For Azure, we can't easily list models without knowing deployment name
            # but we can try a simple request or check endpoint
            return {"ok": True, "provider": self.profile.provider, "endpoint": self.base_url}
        except Exception as exc:
            return {"ok": False, "provider": self.profile.provider, "base_url": self.base_url, "error": str(exc)}
