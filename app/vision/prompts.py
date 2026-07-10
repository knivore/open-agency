"""Prompt builders for ambient-home camera analysis."""

from __future__ import annotations

DEFAULT_SCENE_ANALYSIS_PROMPT = (
    "Analyze this home camera snapshot conservatively. Return a concise summary, "
    "detected objects, estimated people count, safety concerns, suggested actions, "
    "whether lights appear on, whether the scene appears messy, and confidence."
)


def analysis_prompt(question: str | None = None) -> str:
    if not isinstance(question, str) or not question.strip():
        return DEFAULT_SCENE_ANALYSIS_PROMPT
    return f"{DEFAULT_SCENE_ANALYSIS_PROMPT} Focus especially on this question: {question.strip()}"
