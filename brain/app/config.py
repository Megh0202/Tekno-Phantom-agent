from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    llm_mode: Literal["local", "cloud", "anthropic"] = "local"
    log_level: str = "INFO"

    vllm_base_url: str = "http://localhost:8001/v1"
    vllm_api_key: str = "local-key"
    vllm_model: str = "meta-llama/Llama-3.1-8B-Instruct"

    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-20250514"

    brain_api_key: str = ""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
