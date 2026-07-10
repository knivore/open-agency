"""Backend-owned onboarding helpers for the local-first setup flow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.domain import ModelProfileDefinition, ModelProviderDefinition, ModelProviderType, ProviderEndpointDefinition
from app.services.main_agent_setup.service import MainAgentSetupConfig, MainAgentSetupService

if TYPE_CHECKING:
    from app.api.context import ApiContext


@dataclass(slots=True)
class RecommendedAgentSetupResult:
    coder_agent_id: str | None
    embedding_agent_id: str | None
    embedding_model_profile_id: str | None
    evaluation_agent_id: str | None


LOCAL_PROVIDER_IDS = {
    "openai": "setup-provider-openai",
    "ollama": "setup-provider-ollama",
}


@dataclass(slots=True)
class SetupOnboardingService:
    context: ApiContext

    async def ensure_model_profile(
            self,
            *,
            provider_key: str,
            model_name: str,
            api_key: str | None = None,
            base_url: str | None = None,
    ) -> ModelProfileDefinition:
        normalized_provider = provider_key.strip().lower()
        normalized_model = model_name.strip()
        if not normalized_provider:
            raise ValueError("Provider is required.")
        if not normalized_model:
            raise ValueError("Model name is required.")

        if normalized_provider == "openai":
            if not api_key or not api_key.strip():
                raise ValueError("OpenAI setup requires an API key.")
            provider = await self._upsert_provider(
                provider_id=LOCAL_PROVIDER_IDS["openai"],
                name="OpenAI",
                provider_type=ModelProviderType.OPENAI,
                base_url=base_url or "https://api.openai.com/v1",
                description="Created from the local setup flow.",
                config={},
            )
            profile = ModelProfileDefinition(
                id="setup-profile-openai",
                name="OpenAI Main",
                provider=provider.id,
                model=normalized_model,
                base_url=provider.endpoint.base_url if provider.endpoint else base_url,
                api_key_ref=api_key.strip(),
                temperature=0.2,
                max_tokens=400,
                supports_tools=True,
                supports_structured_output=True,
                supports_streaming=True,
            )
            return await self.context.model_profile_repo.save(profile)

        if normalized_provider == "ollama":
            provider = await self._upsert_provider(
                provider_id=LOCAL_PROVIDER_IDS["ollama"],
                name="Ollama",
                provider_type=ModelProviderType.OLLAMA,
                base_url=base_url or "http://localhost:11434",
                description="Created from the local setup flow.",
                config={},
            )
            profile = ModelProfileDefinition(
                id="setup-profile-ollama",
                name="Ollama Main",
                provider=provider.id,
                model=normalized_model,
                base_url=provider.endpoint.base_url if provider.endpoint else base_url,
                temperature=0.2,
                max_tokens=400,
                supports_tools=True,
                supports_structured_output=False,
                supports_streaming=True,
            )
            return await self.context.model_profile_repo.save(profile)

        raise ValueError(f"Unsupported setup provider '{provider_key}'.")

    async def ensure_main_agent(
            self,
            *,
            model_profile_id: str,
            agent_name: str = "Main Agent",
    ):
        service = MainAgentSetupService(self.context)
        existing = await service.get_active_main_agent_profile()
        if existing is not None:
            if existing.default_model_profile_id != model_profile_id:
                return await service.update_active_main_agent_profile(
                    default_model_profile_id=model_profile_id,
                )
            return existing

        return await service.create_main_agent(
            MainAgentSetupConfig(
                agent_name=agent_name.strip() or "Main Agent",
                agent_description="Default conversational orchestrator for this local install.",
                agent_instructions=(
                    "You are the main local Agency assistant. Help the user directly, use tools carefully, "
                    "and ask for approval before risky mutations."
                ),
                model_profile_id=model_profile_id,
                profile_id="main-agent-profile",
            )
        )

    async def ensure_recommended_agents(
            self,
            *,
            include_coder: bool = True,
            include_embedding: bool = True,
            include_evaluation: bool = True,
    ) -> RecommendedAgentSetupResult:
        # Reuse the canonical setup routines that already keep these agent
        # profiles aligned with the prompt docs. This lets browser and terminal
        # onboarding deprecate the old aggregate setup command without forking
        # another provisioning implementation.
        from scripts import setup as setup_script

        coder = await setup_script.setup_coder_agent(context=self.context) if include_coder else None
        embedding = await setup_script.setup_embedding_agent(context=self.context) if include_embedding else None
        evaluation = await setup_script.setup_evaluation_agent(context=self.context) if include_evaluation else None
        return RecommendedAgentSetupResult(
            coder_agent_id=coder.id if coder is not None else None,
            embedding_agent_id=embedding.agent.id if embedding is not None else None,
            embedding_model_profile_id=embedding.model_profile.id if embedding is not None else None,
            evaluation_agent_id=evaluation.agent.id if evaluation is not None else None,
        )

    async def _upsert_provider(
            self,
            *,
            provider_id: str,
            name: str,
            provider_type: ModelProviderType,
            base_url: str,
            description: str,
            config: dict,
    ) -> ModelProviderDefinition:
        provider = ModelProviderDefinition(
            id=provider_id,
            name=name,
            provider_type=provider_type,
            description=description,
            endpoint=ProviderEndpointDefinition(base_url=base_url),
            config=config,
        )
        return await self.context.model_provider_repo.save(provider)
