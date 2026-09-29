from app.config import Settings
from app.llm.anthropic_client import AnthropicLLMClient
from app.llm.base import LLMClient


def build_llm_client(settings: Settings) -> LLMClient:
    if settings.llm_provider == "anthropic":
        return AnthropicLLMClient(settings)
    if settings.llm_provider == "fake":
        from app.dev.fakes import FakeLLMClient

        return FakeLLMClient()
    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider}")
