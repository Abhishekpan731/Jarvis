"""Build the configured provider, or report that none is configured."""

from app.ai.base import LanguageModel, NotConfigured
from app.ai.openai_provider import OpenAIModel
from app.config import settings


def get_model() -> LanguageModel:
    if not settings.llm_configured:
        raise NotConfigured("No model provider credential is set.")
    if settings.ai_provider != "openai":
        raise NotConfigured(f"Provider '{settings.ai_provider}' is not implemented.")
    return OpenAIModel(
        api_key=settings.resolved_api_key,
        model=settings.resolved_model,
        base_url=settings.openai_base_url,
    )


def provider_status() -> dict:
    return {
        "configured": settings.llm_configured,
        "provider": settings.ai_provider if settings.llm_configured else None,
        "model": settings.resolved_model if settings.llm_configured else None,
    }
