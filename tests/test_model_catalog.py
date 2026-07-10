from app.llm.fallback import CURATED_FALLBACK_MODEL_CAPABILITIES, DEFAULT_AUTO_FALLBACK_MODELS
from app.services.models import CURATED_MODEL_OPTIONS


def test_gpt_5_6_family_is_available_for_openai_and_codex() -> None:
    expected = {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}

    for provider in ("openai", "openai_codex"):
        available = {option["id"] for option in CURATED_MODEL_OPTIONS[provider]}
        assert expected.issubset(available)


def test_codex_fallback_prefers_current_efficient_model() -> None:
    assert DEFAULT_AUTO_FALLBACK_MODELS["openai_codex"] == ("gpt-5.6-luna", "gpt-5.4-mini")
    assert CURATED_FALLBACK_MODEL_CAPABILITIES["openai_codex"]["gpt-5.6-luna"]["tools"] is True
