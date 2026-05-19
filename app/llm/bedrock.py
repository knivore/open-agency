from __future__ import annotations

import boto3
from typing import Any, Dict, Iterator, List, Optional

from app.domain import ModelProfileDefinition
from app.llm.base import ModelMessage, ModelResponse
from app.llm.registry import LLMEnvironmentConfig


class BedrockModelClient:
    provider_key = "aws_bedrock"

    def __init__(self, profile: ModelProfileDefinition, env_config: LLMEnvironmentConfig):
        self.profile = profile
        region = profile.framework_hints.adapter_config.get("aws_region") or "us-east-1"
        self.client = boto3.client("bedrock-runtime", region_name=region)

    def generate_text(
            self,
            messages: List[ModelMessage],
            *,
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> ModelResponse:
        raise NotImplementedError("Bedrock text generation is not implemented yet")

    def generate_structured(
            self,
            messages: List[ModelMessage],
            *,
            schema: Dict[str, Any],
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> ModelResponse:
        raise NotImplementedError("Bedrock structured generation is not implemented yet")

    def stream_text(
            self,
            messages: List[ModelMessage],
            *,
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> Iterator[str]:
        raise NotImplementedError("Bedrock streaming is not implemented yet")

    def count_tokens(self, messages: List[ModelMessage], **kwargs: Any) -> Optional[int]:
        return None

    def health_check(self) -> Dict[str, Any]:
        try:
            self.client.list_foundation_models()
            return {"ok": True, "provider": self.profile.provider, "model": self.profile.model}
        except Exception as exc:
            return {"ok": False, "provider": self.profile.provider, "model": self.profile.model, "error": str(exc)}
