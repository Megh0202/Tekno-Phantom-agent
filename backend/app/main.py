from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Header, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from app.brain.http_client import HttpBrainClient
from app.config import Settings, get_settings
from app.mcp.browser_client import build_browser_client
from app.mcp.filesystem_client import build_filesystem_client
from app.runtime.executor import AgentExecutor
from app.runtime.instruction_parser import parse_structured_task_steps
from app.runtime.plan_normalizer import build_recovery_steps, normalize_plan_steps
from app.runtime.selector_memory import build_selector_memory_store
from app.runtime.step_importer import StepImportError, parse_step_rows_from_upload
from app.runtime.store import build_run_store
from app.runtime.test_case_store import build_test_case_store
from app.schemas import (
    CancelRunResponse,
    PlanGenerateRequest,
    PlanGenerateResponse,
    RunCreateRequest,
    RunListResponse,
    RunState,
    StepImportResponse,
    TestCaseCreateRequest,
    TestCaseListResponse,
    TestCaseState,
)

LOGGER = logging.getLogger("tekno.phantom.api")

# Playwright requires subprocess support; on Windows this must be Proactor loop.
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    prefix = "bearer "
    if not authorization.lower().startswith(prefix):
        return None
    token = authorization[len(prefix) :].strip()
    return token or None


def _sanitize_plan_steps(
    steps: list[dict[str, object]],
    *,
    start_url: str | None,
) -> list[dict[str, object]]:
    """
    Drop clearly generic placeholder assertions that are unrelated to the target site.
    """
    normalized_url = (start_url or "").lower()
    is_example_site = "example.com" in normalized_url

    sanitized: list[dict[str, object]] = []
    for step in steps:
        if step.get("type") == "verify_text":
            raw_value = str(step.get("value", "")).strip().lower()
            if raw_value in {"example", "example domain"} and not is_example_site:
                continue
        sanitized.append(step)

    return sanitized


def _ensure_drag_step(task: str, steps: list[dict[str, object]]) -> list[dict[str, object]]:
    def _contains_short_answer(text: str) -> bool:
        candidate = text.lower()
        return any(token in candidate for token in ("short answer", "short-answer", "short_answer"))

    def _is_drag_candidate(step: dict[str, object]) -> bool:
        if step.get("type") not in {"click", "select"}:
            return False
        fields = [
            str(step.get("selector", "")),
            str(step.get("target", "")),
            str(step.get("value", "")),
            str(step.get("option", "")),
            str(step.get("text", "")),
        ]
        return _contains_short_answer(" ".join(fields))

    def _drag_insert_index(items: list[dict[str, object]]) -> int:
        for index, step in enumerate(items):
            step_type = str(step.get("type") or "").lower()
            selector = str(step.get("selector", "")).lower()
            text = str(step.get("text", "")).lower()
            value = str(step.get("value", "")).lower()
            if step_type == "type" and any(
                token in selector or token in text
                for token in ("label", "first name", "form name", "formname")
            ):
                return index
            if step_type == "click" and any(token in selector for token in ("save", "required")):
                return index
            if step_type == "verify_text" and "create form" in value:
                return index
        return len(items)

    task_lower = task.lower()
    mentions_drag = "drag" in task_lower
    mentions_drop = "drop" in task_lower
    mentions_short_answer = _contains_short_answer(task_lower)
    should_force_drag = (mentions_drag and (mentions_drop or mentions_short_answer)) or mentions_short_answer

    ensured = [dict(step) for step in steps]
    insert_at = _drag_insert_index(ensured)

    existing_drag_indices = [index for index, step in enumerate(ensured) if step.get("type") == "drag"]
    if existing_drag_indices:
        first_drag_index = existing_drag_indices[0]
        if first_drag_index > insert_at:
            drag_step = dict(ensured.pop(first_drag_index))
            ensured.insert(insert_at, drag_step)
        return ensured

    drag_candidate_index = next(
        (index for index, step in enumerate(ensured) if _is_drag_candidate(step)),
        None,
    )
    if drag_candidate_index is not None:
        candidate = ensured.pop(drag_candidate_index)
        source_selector = (
            str(candidate.get("selector"))
            if candidate.get("selector")
            else str(candidate.get("target") or "short answer")
        )
        insert_at = _drag_insert_index(ensured)
        ensured.insert(
            insert_at,
            {
                "type": "drag",
                "source_selector": source_selector,
                "target_selector": "form canvas",
            },
        )
        return ensured

    if not should_force_drag:
        return ensured

    ensured.insert(
        insert_at,
        {
            "type": "drag",
            "source_selector": "short answer",
            "target_selector": "form canvas",
        },
    )
    return ensured


def _expand_drag_steps(
    steps: list[dict[str, object]],
    *,
    max_steps: int,
) -> list[dict[str, object]]:
    """
    Expand each drag step into explicit actions so runtime and UI show:
    click(select) -> drag -> wait(drop-settle).
    """
    expanded: list[dict[str, object]] = []

    for step in steps:
        if len(expanded) >= max_steps:
            break

        if str(step.get("type", "")).lower() != "drag":
            expanded.append(dict(step))
            continue

        source_selector = str(step.get("source_selector") or "").strip()
        if not source_selector:
            expanded.append(dict(step))
            continue

        prev = expanded[-1] if expanded else None
        prev_is_same_click = bool(
            prev
            and str(prev.get("type", "")).lower() == "click"
            and str(prev.get("selector") or "").strip() == source_selector
        )

        if not prev_is_same_click and len(expanded) < max_steps:
            expanded.append({"type": "click", "selector": source_selector})

        if len(expanded) < max_steps:
            expanded.append(dict(step))

        if len(expanded) < max_steps:
            expanded.append({"type": "wait", "until": "timeout", "ms": 120})

    return expanded[:max_steps]


def build_admin_auth_dependency(settings: Settings):
    async def require_admin_auth(
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
    ) -> None:
        if not settings.admin_api_token:
            return

        provided = x_admin_token or _extract_bearer_token(authorization)
        if provided != settings.admin_api_token:
            raise HTTPException(status_code=401, detail="Unauthorized: invalid admin token")

    return require_admin_auth


def build_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(title="Tekno Phantom Agent API", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    run_store = build_run_store(settings)
    test_case_store = build_test_case_store(settings)
    selector_memory = build_selector_memory_store(settings)
    brain_client = HttpBrainClient(settings)
    browser_client = build_browser_client(settings)
    file_client = build_filesystem_client(settings)
    executor = AgentExecutor(
        settings,
        brain_client,
        run_store,
        browser_client,
        file_client,
        selector_memory_store=selector_memory,
    )
    require_admin_auth = build_admin_auth_dependency(settings)

    @app.on_event("shutdown")
    async def shutdown_event() -> None:
        await file_client.aclose()

    @app.get("/health")
    async def health() -> dict:
        provider_health = await brain_client.healthcheck()
        return {
            "status": "ok",
            "llm": provider_health,
            "mode": provider_health.get("mode", "unknown"),
            "browser_mode": settings.browser_mode,
            "filesystem_mode": settings.filesystem_mode,
            "run_store_backend": settings.run_store_backend,
            "max_steps_per_run": settings.max_steps_per_run,
        }

    @app.get("/api/config")
    async def api_config() -> dict:
        health = await brain_client.healthcheck()
        return {
            "llm_mode": health.get("mode", "unknown"),
            "model": health.get("model", "unknown"),
            "browser_mode": settings.browser_mode,
            "filesystem_mode": settings.filesystem_mode,
            "run_store_backend": settings.run_store_backend,
            "max_steps_per_run": settings.max_steps_per_run,
            "admin_auth_required": bool(settings.admin_api_token),
        }

    @app.post("/api/runs", response_model=RunState)
    async def create_run(
        request: RunCreateRequest,
        background_tasks: BackgroundTasks,
        _: None = Depends(require_admin_auth),
    ) -> RunState:
        raw_steps = [step.model_dump(exclude_none=True) for step in request.steps]
        expanded_steps = _expand_drag_steps(raw_steps, max_steps=settings.max_steps_per_run)
        expanded_request = RunCreateRequest.model_validate(
            {
                "run_name": request.run_name,
                "start_url": request.start_url,
                "steps": expanded_steps,
                "test_data": request.test_data,
                "selector_profile": request.selector_profile,
            }
        )

        if len(expanded_request.steps) > settings.max_steps_per_run:
            raise HTTPException(
                status_code=400,
                detail=f"Step count exceeds max_steps_per_run={settings.max_steps_per_run}",
            )

        run = run_store.create(expanded_request)
        background_tasks.add_task(executor.execute, run.run_id)
        LOGGER.info("Run created: %s", run.run_id)
        return run

    @app.post("/api/test-cases", response_model=TestCaseState)
    async def create_test_case(
        request: TestCaseCreateRequest,
        _: None = Depends(require_admin_auth),
    ) -> TestCaseState:
        if len(request.steps) > settings.max_steps_per_run:
            raise HTTPException(
                status_code=400,
                detail=f"Step count exceeds max_steps_per_run={settings.max_steps_per_run}",
            )
        test_case = test_case_store.create(request)
        return test_case

    @app.post("/api/test-cases/import", response_model=StepImportResponse)
    async def import_test_case_steps(
        file: UploadFile = File(...),
        run_name: str | None = Form(default=None),
        start_url: str | None = Form(default=None),
        _: None = Depends(require_admin_auth),
    ) -> StepImportResponse:
        filename = (file.filename or "imported_steps.csv").strip() or "imported_steps.csv"
        payload = await file.read()
        await file.close()

        try:
            raw_steps = parse_step_rows_from_upload(filename, payload)
        except StepImportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        normalized_steps = normalize_plan_steps(
            raw_steps,
            max_steps=max(len(raw_steps), settings.max_steps_per_run, 1),
        )
        normalized_steps = _expand_drag_steps(normalized_steps, max_steps=settings.max_steps_per_run)
        if not normalized_steps:
            raise HTTPException(
                status_code=422,
                detail=(
                    "No runnable steps were recognized in the uploaded file. "
                    "Include at least one supported action row."
                ),
            )

        if len(normalized_steps) > settings.max_steps_per_run:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Imported step count exceeds max_steps_per_run={settings.max_steps_per_run}. "
                    "Reduce rows in the file or increase max limit."
                ),
            )

        default_run_name = Path(filename).stem.strip().replace(" ", "_") or "imported_test_case"
        resolved_run_name = (run_name or "").strip() or default_run_name
        resolved_start_url = (start_url or "").strip() or None

        try:
            validated = RunCreateRequest.model_validate(
                {
                    "run_name": resolved_run_name,
                    "start_url": resolved_start_url,
                    "steps": normalized_steps,
                    "test_data": {},
                    "selector_profile": {},
                }
            )
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Imported steps are invalid: {exc}") from exc

        return StepImportResponse(
            run_name=validated.run_name,
            start_url=validated.start_url,
            steps=validated.steps,
            source_filename=filename,
            imported_count=len(validated.steps),
        )

    @app.get("/api/test-cases", response_model=TestCaseListResponse)
    async def list_test_cases() -> TestCaseListResponse:
        return TestCaseListResponse(items=test_case_store.list())

    @app.get("/api/test-cases/{test_case_id}", response_model=TestCaseState)
    async def get_test_case(test_case_id: str) -> TestCaseState:
        test_case = test_case_store.get(test_case_id)
        if not test_case:
            raise HTTPException(status_code=404, detail="Test case not found")
        return test_case

    @app.post("/api/test-cases/{test_case_id}/run", response_model=RunState)
    async def run_test_case(
        test_case_id: str,
        background_tasks: BackgroundTasks,
        _: None = Depends(require_admin_auth),
    ) -> RunState:
        test_case = test_case_store.get(test_case_id)
        if not test_case:
            raise HTTPException(status_code=404, detail="Test case not found")

        run_request = RunCreateRequest.model_validate(
            {
                "run_name": test_case.name,
                "start_url": test_case.start_url,
                "steps": [step.model_dump(exclude_none=True) for step in test_case.steps],
                "test_data": test_case.test_data,
                "selector_profile": test_case.selector_profile,
            }
        )
        expanded_steps = _expand_drag_steps(
            [step.model_dump(exclude_none=True) for step in run_request.steps],
            max_steps=settings.max_steps_per_run,
        )
        expanded_run_request = RunCreateRequest.model_validate(
            {
                "run_name": run_request.run_name,
                "start_url": run_request.start_url,
                "steps": expanded_steps,
                "test_data": run_request.test_data,
                "selector_profile": run_request.selector_profile,
            }
        )
        run = run_store.create(expanded_run_request)
        background_tasks.add_task(executor.execute, run.run_id)
        LOGGER.info("Run created from test case: %s (test_case_id=%s)", run.run_id, test_case_id)
        return run

    @app.post("/api/plan", response_model=PlanGenerateResponse)
    async def generate_plan(
        request: PlanGenerateRequest,
        _: None = Depends(require_admin_auth),
    ) -> PlanGenerateResponse:
        max_steps = request.max_steps or settings.max_steps_per_run
        max_steps = min(max_steps, settings.max_steps_per_run)
        parsed_steps = parse_structured_task_steps(request.task, max_steps=max_steps)
        if parsed_steps:
            validated = RunCreateRequest.model_validate(
                {
                    "run_name": "prompt-steps-run",
                    "start_url": None,
                    "steps": parsed_steps,
                    "test_data": request.test_data,
                    "selector_profile": request.selector_profile,
                }
            )
            return PlanGenerateResponse(
                run_name=validated.run_name,
                start_url=validated.start_url,
                steps=validated.steps[:max_steps],
            )

        planning_task = (
            f"{request.task}\n\n"
            "Planner constraints:\n"
            "- Return only runnable steps supported by this runtime.\n"
            "- Supported step types: navigate, click, type, select, drag, scroll, wait, handle_popup, verify_text, verify_image.\n"
            "- Cover every explicit user instruction in order when max_steps allows.\n"
            "- Do not invent extra requirements that are not explicitly requested.\n"
            "- Use Playwright-compatible selectors only.\n"
            "- Never use jQuery ':contains(...)'. Use 'text=...' or ':has-text(\"...\")' instead.\n"
            "- Prefer stable selectors: id, name, data-testid, role/label-based selectors.\n"
            "- Avoid brittle selectors such as exact list indexes ('data-index=0', ':first-child') when possible.\n"
            "- Do not use generic verification selectors like 'h1' or 'body' when checking specific controls.\n"
            "- For checks like 'Create Form button is visible', use button selectors with id/name or :has-text.\n"
            "- Keep action order aligned to the prompt; do not verify post-login controls before login actions complete.\n"
            "- For login pages, prefer '#username' or input[name='username'] for username/email fields,\n"
            "  and '#password' or input[name='password'] for password fields if present.\n"
        )

        if request.test_data:
            test_keys = ", ".join(sorted(request.test_data.keys()))
            planning_task = (
                f"{planning_task}\n\n"
                "Test data keys available:\n"
                f"{test_keys}\n"
                "If needed, reference values with placeholders like {{email}} and {{password}}. "
                "Do not replace explicit values already written in the task."
            )
        if request.selector_profile:
            planning_task = (
                f"{planning_task}\n\n"
                "Selector profile (JSON):\n"
                f"{json.dumps(request.selector_profile, ensure_ascii=False)}\n"
                "Prefer these selectors for matching fields."
            )

        try:
            payload = await brain_client.plan_task(planning_task, max_steps=max_steps)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Brain plan generation failed: {exc}") from exc

        try:
            normalized_steps = normalize_plan_steps(payload.get("steps"), max_steps=max_steps)
            normalized_steps = _sanitize_plan_steps(normalized_steps, start_url=payload.get("start_url"))
            normalized_steps = _ensure_drag_step(request.task, normalized_steps)
            normalized_steps = _expand_drag_steps(normalized_steps, max_steps=max_steps)
            if not normalized_steps:
                normalized_steps = build_recovery_steps(request.task, max_steps=max_steps)
            if not normalized_steps:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Could not generate runnable steps from this prompt. "
                        "Please include a URL and clearer element targets."
                    ),
                )
            validated = RunCreateRequest.model_validate(
                {
                    "run_name": payload.get("run_name", "ai-generated-run"),
                    "start_url": payload.get("start_url"),
                    "steps": normalized_steps,
                    "test_data": request.test_data,
                    "selector_profile": request.selector_profile,
                }
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Invalid plan returned by brain: {exc}") from exc

        trimmed_steps = validated.steps[:max_steps]
        return PlanGenerateResponse(
            run_name=validated.run_name,
            start_url=validated.start_url,
            steps=trimmed_steps,
        )

    @app.get("/api/runs", response_model=RunListResponse)
    async def list_runs() -> RunListResponse:
        return RunListResponse(items=run_store.list())

    @app.get("/api/runs/{run_id}", response_model=RunState)
    async def get_run(run_id: str) -> RunState:
        run = run_store.get(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        return run

    @app.post("/api/runs/{run_id}/cancel", response_model=CancelRunResponse)
    async def cancel_run(run_id: str, _: None = Depends(require_admin_auth)) -> CancelRunResponse:
        run = run_store.mark_cancelled(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        return CancelRunResponse(run_id=run_id, status=run.status)

    return app


app = build_app()
