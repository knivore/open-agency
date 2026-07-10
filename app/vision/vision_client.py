"""Vision provider abstraction for ambient camera analysis."""

from __future__ import annotations

import base64
import json
from openai import OpenAI
from typing import Any

from app.core.config import Settings, get_settings
from .prompts import analysis_prompt


class VisionClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def analyse_image(self, image_bytes: bytes, *, media_type: str, question: str | None = None) -> dict[str, Any]:
        provider = self.settings.agency_vision_provider.strip().lower()
        if provider == "openai" and self.settings.openai_api_key:
            return self._analyse_with_openai(image_bytes, media_type=media_type, question=question)
        return self._analyse_locally(question=question)

    def _analyse_locally(self, *, question: str | None = None) -> dict[str, Any]:
        summary = "Snapshot captured successfully. Local vision provider is configured without image understanding."
        if isinstance(question, str) and question.strip():
            summary = f"{summary} Requested question: {question.strip()}"
        return {
            "summary": summary,
            "detected_objects": [],
            "detected_people_count": 0,
            "safety_concerns": [],
            "suggested_actions": [],
            "lights_on": None,
            "room_messy": None,
            "confidence": "low",
        }

    def _analyse_with_openai(self, image_bytes: bytes, *, media_type: str, question: str | None = None) -> dict[
        str, Any]:
        client = OpenAI(
            api_key=self.settings.openai_api_key,
            base_url=self.settings.openai_api_base_url,
        )
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        response = client.chat.completions.create(
            model=self.settings.agency_vision_model or "gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": "You analyze home camera imagery and return strict JSON only.",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": analysis_prompt(question)},
                        {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_b64}"}},
                    ],
                },
            ],
            max_tokens=500,
        )
        content = response.choices[0].message.content or "{}"
        return json.loads(content)
