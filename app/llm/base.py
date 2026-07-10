from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Literal, Optional, Protocol

ModelRole = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class ModelToolCall:
    id: Optional[str]
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    raw: Any = None


@dataclass(slots=True)
class ModelMessage:
    role: ModelRole
    content: Any
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    # OpenAI-compatible chat APIs require tool results to follow an assistant message
    # carrying the exact tool_calls they answer.
    tool_calls: List[ModelToolCall] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ModelResponse:
    content: Any
    tool_calls: List[ModelToolCall] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    raw_response: Any = None
    provider: str = ""
    model: str = ""
    latency_ms: float = 0.0


class BaseModelClient(Protocol):
    provider_key: str

    def generate_text(
            self,
            messages: List[ModelMessage],
            *,
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> ModelResponse: ...

    def generate_structured(
            self,
            messages: List[ModelMessage],
            *,
            schema: Dict[str, Any],
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> ModelResponse: ...

    def stream_text(
            self,
            messages: List[ModelMessage],
            *,
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> Iterator[str]: ...

    def count_tokens(self, messages: List[ModelMessage], **kwargs: Any) -> Optional[int]: ...

    def embed_texts(self, texts: List[str], **kwargs: Any) -> List[List[float]]: ...

    def health_check(self) -> Dict[str, Any]: ...
