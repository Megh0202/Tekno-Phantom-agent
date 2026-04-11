from typing import Any

from pydantic import BaseModel, Field


class SummarizeRequest(BaseModel):
    content: str = Field(min_length=1, max_length=10000)


class SummarizeResponse(BaseModel):
    summary: str


class PlanRequest(BaseModel):
    task: str = Field(min_length=1, max_length=5000)
    max_steps: int = Field(default=300, ge=1, le=500)


class PlanResponse(BaseModel):
    run_name: str
    start_url: str | None = None
    steps: list[dict[str, Any]] = Field(min_length=1)
    raw_llm_response: str | None = None


class SelectorSuggestionRequest(BaseModel):
    step_type: str = Field(min_length=1, max_length=40)
    failed_selector: str = Field(min_length=1, max_length=2000)
    error_message: str = Field(min_length=1, max_length=2000)
    page: dict[str, Any]
    text_hint: str | None = Field(default=None, max_length=500)
    max_candidates: int = Field(default=3, ge=1, le=8)
    # Semantic fingerprint of the target element captured by the perception
    # layer before the selector was attempted.  Provides the LLM with a
    # precise identity description (tag, role, visible text, aria-label,
    # testid, name, placeholder) so it can ground suggestions directly.
    element_hint: dict[str, Any] | None = Field(default=None)


class SelectorSuggestionResponse(BaseModel):
    selectors: list[str] = Field(default_factory=list)
