from __future__ import annotations

import logging
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException

from app.config import get_settings
from app.llm.factory import build_llm_provider
from app.schemas import (
    DiagnoseFailureRequest,
    DiagnoseFailureResponse,
    HumanStepsRequest,
    HumanStepsResponse,
    PlanRequest,
    PlanResponse,
    SelectorSuggestionRequest,
    SelectorSuggestionResponse,
    SummarizeRequest,
    SummarizeResponse,
)

LOGGER = logging.getLogger("tekno.phantom.brain")


def build_app() -> FastAPI:
    settings = get_settings()
    log_level_name = str(settings.log_level).upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    logging.basicConfig(level=log_level)

    provider = build_llm_provider(settings)
    app = FastAPI(title="Tekno Phantom Brain", version="0.1.0")

    def ensure_auth(authorization: str | None) -> None:
        if not settings.brain_api_key:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing bearer token")
        token = authorization.removeprefix("Bearer ").strip()
        if token != settings.brain_api_key:
            raise HTTPException(status_code=401, detail="Invalid bearer token")

    @app.get("/health")
    async def health(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, str]:
        ensure_auth(authorization)
        status = await provider.healthcheck()
        return status

    @app.post("/v1/summarize", response_model=SummarizeResponse)
    async def summarize(
        request: SummarizeRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> SummarizeResponse:
        ensure_auth(authorization)
        LOGGER.debug("Summarize request received with %s chars", len(request.content))
        summary = await provider.summarize(request.content)
        return SummarizeResponse(summary=summary)

    @app.post("/v1/plan", response_model=PlanResponse)
    async def plan(
        request: PlanRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> PlanResponse:
        ensure_auth(authorization)
        LOGGER.debug("Plan request received with %s chars", len(request.task))
        payload = await provider.plan_task(request.task, request.max_steps)
        return PlanResponse.model_validate(payload)

    @app.post("/v1/selector-suggestions", response_model=SelectorSuggestionResponse)
    async def selector_suggestions(
        request: SelectorSuggestionRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> SelectorSuggestionResponse:
        ensure_auth(authorization)
        selectors = await provider.suggest_selectors(
            step_type=request.step_type,
            failed_selector=request.failed_selector,
            error_message=request.error_message,
            page=request.page,
            text_hint=request.text_hint,
            max_candidates=request.max_candidates,
            element_hint=request.element_hint,
        )
        return SelectorSuggestionResponse(selectors=selectors[: request.max_candidates])

    @app.post("/v1/human-steps", response_model=HumanStepsResponse)
    async def human_steps(
        request: HumanStepsRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> HumanStepsResponse:
        ensure_auth(authorization)
        steps = await provider.human_steps(request.prompt, request.max_steps)
        return HumanStepsResponse(steps=steps)

    @app.post("/v1/diagnose-failure", response_model=DiagnoseFailureResponse)
    async def diagnose_failure(
        request: DiagnoseFailureRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> DiagnoseFailureResponse:
        ensure_auth(authorization)
        result = await provider.diagnose_failure(
            step_type=request.step_type,
            error_message=request.error_message,
            screenshot_base64=request.screenshot_base64,
            goal=request.goal,
        )
        return DiagnoseFailureResponse(
            diagnosis=result["diagnosis"],
            suggested_fix=result["suggested_fix"],
        )

    return app


app = build_app()
