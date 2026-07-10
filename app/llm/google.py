from __future__ import annotations

import httpx
import json
import time
from typing import Any, Dict, Iterator, List, Optional

from app.domain import ModelProfileDefinition
from app.llm.base import ModelMessage, ModelResponse
from app.llm.registry import LLMEnvironmentConfig


class GoogleModelClient:
    provider_key = "google"

    def __init__(self, profile: ModelProfileDefinition, env_config: LLMEnvironmentConfig):
        self.profile = profile
        self.env_config = env_config
        self.base_url = profile.base_url or "https://generativelanguage.googleapis.com"

        # Support for OAuth tokens
        self.config = profile.parameters or {}
        self.access_token = self.config.get("access_token")
        self.refresh_token = self.config.get("refresh_token")
        self.expires_at = self.config.get("expires_at")

        self.api_key = profile.api_key_ref or env_config.google_api_key

    async def _ensure_authorized(self):
        """Ensure token is valid if using OAuth flow"""
        if self.access_token:
            # Check if token is expired (with 5 min buffer)
            if self.expires_at and time.time() > (self.expires_at - 300):
                if self.refresh_token:
                    from app.utils.oauth_pkce import OAuthPKCEHandler
                    client_id = self.config.get("client_id") or "DEFAULT_CLIENT_ID"
                    handler = OAuthPKCEHandler.for_provider("google", client_id=client_id, redirect_uri="")
                    new_tokens = await handler.refresh_token(self.refresh_token)
                    self.access_token = new_tokens["access_token"]
                    self.refresh_token = new_tokens.get("refresh_token", self.refresh_token)
                    self.expires_at = time.time() + new_tokens.get("expires_in", 3600)

                    # Persist refreshed tokens to database
                    if self.env_config.model_provider_repo:
                        await self.env_config.model_provider_repo.update_tokens(
                            self.profile.provider_id,
                            self.access_token,
                            self.refresh_token,
                            self.expires_at
                        )
                else:
                    # Token expired and no refresh token
                    raise RuntimeError(
                        "Google OAuth token expired. Please re-authorize via 'POST /model-providers/{id}/authorize'")

    def _ensure_api_key(self) -> str:
        if self.access_token:
            return self.access_token
        if not self.api_key:
            raise ValueError("GOOGLE_API_KEY or OAuth access_token is not configured")
        return self.api_key

    def _get_headers(self) -> Dict[str, str]:
        headers = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def _get_params(self, api_key: str) -> Dict[str, str]:
        if self.access_token:
            return {}
        return {"key": api_key}

    def _payload(self, messages: List[ModelMessage], temperature: Optional[float], max_tokens: Optional[int]) -> Dict[
        str, Any]:
        contents = []
        for message in messages:
            contents.append(
                {
                    "role": "model" if message.role == "assistant" else "user",
                    "parts": [{"text": str(message.content)}],
                }
            )
        payload: Dict[str, Any] = {"contents": contents}
        generation_config: Dict[str, Any] = {}
        if temperature is not None or self.profile.temperature is not None:
            generation_config["temperature"] = temperature if temperature is not None else self.profile.temperature
        if max_tokens is not None or self.profile.max_tokens is not None:
            generation_config["maxOutputTokens"] = max_tokens if max_tokens is not None else self.profile.max_tokens
        if generation_config:
            payload["generationConfig"] = generation_config
        return payload

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
        api_key = self._ensure_api_key()
        response = httpx.post(
            f"{self.base_url.rstrip('/')}/v1beta/models/{self.profile.model}:generateContent",
            params=self._get_params(api_key),
            headers=self._get_headers(),
            json=self._payload(messages, temperature, max_tokens),
            timeout=60.0,
        )
        response.raise_for_status()
        data = response.json()
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        content = "".join(part.get("text", "") for part in parts)
        return ModelResponse(
            content=content,
            tool_calls=[],
            usage=data.get("usageMetadata", {}),
            raw_response=data,
            provider=self.profile.provider,
            model=self.profile.model,
            latency_ms=(time.perf_counter() - started_at) * 1000,
        )

    def generate_structured(
            self,
            messages: List[ModelMessage],
            *,
            schema: Dict[str, Any],
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> ModelResponse:
        response = self.generate_text(messages, temperature=temperature, max_tokens=max_tokens, **kwargs)
        if isinstance(response.content, str):
            try:
                response.content = json.loads(response.content)
            except json.JSONDecodeError:
                pass
        return response

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
        api_key = self._ensure_api_key()
        params = self._get_params(api_key)
        params["alt"] = "sse"
        with httpx.stream(
                "POST",
                f"{self.base_url.rstrip('/')}/v1beta/models/{self.profile.model}:streamGenerateContent",
                params=params,
                headers=self._get_headers(),
                json=self._payload(messages, temperature, max_tokens),
                timeout=60.0,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = json.loads(line[5:].strip())
                candidates = data.get("candidates", [])
                if not candidates:
                    continue
                parts = candidates[0].get("content", {}).get("parts", [])
                for part in parts:
                    text = part.get("text")
                    if text:
                        yield text

    def count_tokens(self, messages: List[ModelMessage], **kwargs: Any) -> Optional[int]:
        return None

    def health_check(self) -> Dict[str, Any]:
        try:
            import asyncio
            asyncio.run(self._ensure_authorized())
            api_key = self._ensure_api_key()
            response = httpx.get(
                f"{self.base_url.rstrip('/')}/v1beta/models",
                params=self._get_params(api_key),
                headers=self._get_headers(),
                timeout=5.0,
            )
            return {
                "ok": response.status_code < 500,
                "provider": self.profile.provider,
                "base_url": self.base_url,
                "status_code": response.status_code,
            }
        except Exception as exc:
            return {"ok": False, "provider": self.profile.provider, "base_url": self.base_url, "error": str(exc)}
