from app.config import Settings
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import LLMProvider
from app.llm.local_vllm import LocalVLLMProvider
from app.llm.openai_provider import OpenAIProvider


def build_llm_provider(settings: Settings) -> LLMProvider:
    if settings.llm_mode == "anthropic":
        return AnthropicProvider(settings)
    if settings.llm_mode == "cloud":
        return OpenAIProvider(settings)
    return LocalVLLMProvider(settings)
