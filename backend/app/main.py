from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Header, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.brain.http_client import HttpBrainClient
from app.config import Settings, get_settings
from app.database import init_auth_database
from app.mcp.browser_client import build_browser_client
from app.mcp.filesystem_client import build_filesystem_client
from app.routes import auth_router
from app.runtime.executor import AgentExecutor
from app.runtime.instruction_parser import parse_structured_task_steps
from app.runtime.plan_normalizer import build_recovery_steps, normalize_plan_steps
from app.runtime.selector_memory import build_selector_memory_store
from app.runtime.suite_executor import SuiteExecutor
from app.runtime.suite_store import build_suite_store
from app.runtime.step_importer import StepImportError, parse_step_rows_from_upload
from app.runtime.store import build_run_store
from app.runtime.test_case_store import build_test_case_store
from app.schemas import (
    CancelSuiteRunResponse,
    CancelRunResponse,
    FolderCreateRequest,
    FolderListResponse,
    FolderState,
    PlanGenerateRequest,
    PlanGenerateResponse,
    RunCreateRequest,
    RunListResponse,
    RunState,
    SuiteRunCreateRequest,
    SuiteRunListResponse,
    SuiteRunState,
    StepImportResponse,
    SelectorRecoveryRequest,
    TestCaseCreateRequest,
    TestCaseListResponse,
    TestCaseState,
    TestCaseUpdateRequest,
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
    Harden and sanitize generated/parsed steps so they are more runnable across
    diverse applications and less likely to be brittle.
    """
    normalized_url = (start_url or "").lower()
    has_explicit_start_url = bool((start_url or "").strip())
    is_example_site = "example.com" in normalized_url

    def _clean_text(value: object) -> str:
        return str(value or "").strip()

    def _normalize_selector(raw: object) -> str:
        selector = _clean_text(raw).strip().rstrip(".,;")
        if not selector:
            return ""
        if len(selector) >= 2 and selector[0] == selector[-1] and selector[0] in {"'", '"', "`"}:
            selector = selector[1:-1].strip()
        return selector

    def _looks_like_explicit_selector(value: str) -> bool:
        lowered = value.lower()
        if lowered.startswith(("text=", "xpath=", "css=", "id=", "role=", "label=", "placeholder=")):
            return True
        if lowered in {
            "html",
            "body",
            "main",
            "form",
            "button",
            "input",
            "select",
            "textarea",
            "label",
            "a",
            "div",
            "span",
            "p",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        }:
            return True
        if value.startswith("//"):
            return True
        return any(token in value for token in ("#", ".", "[", "]", ">", "=", ":", "/"))

    def _to_click_selector(raw: object) -> str:
        selector = _normalize_selector(raw)
        if not selector:
            return ""
        if _looks_like_explicit_selector(selector) or selector.startswith("{{"):
            return selector
        return f"text={selector}"

    def _to_text_wait_selector(raw: object) -> str:
        selector = _normalize_selector(raw)
        if not selector:
            return ""
        if _looks_like_explicit_selector(selector) or selector.startswith("{{"):
            return selector
        return f"text={selector}"

    generic_click_targets = {"body", "html", "main", "h1", "h2", "h3"}

    sanitized: list[dict[str, object]] = []
    seen_step_tokens: set[str] = set()
    for step in steps:
        step_type = _clean_text(step.get("type")).lower()
        if not step_type:
            continue

        normalized_step = dict(step)
        normalized_step["type"] = step_type

        if step_type in {"click", "type", "select", "verify_text", "wait", "handle_popup"}:
            if "selector" in normalized_step:
                normalized_step["selector"] = _normalize_selector(normalized_step.get("selector"))

        if step_type == "click":
            selector = _to_click_selector(normalized_step.get("selector"))
            if not selector:
                continue
            if selector.lower() in generic_click_targets:
                continue
            normalized_step["selector"] = selector

        if step_type == "type":
            selector = _to_text_wait_selector(normalized_step.get("selector"))
            text_value = _clean_text(normalized_step.get("text"))
            if not selector or not text_value:
                continue
            normalized_step["selector"] = selector
            normalized_step["text"] = text_value
            normalized_step["clear_first"] = bool(normalized_step.get("clear_first", True))

        if step_type == "select":
            selector = _to_text_wait_selector(normalized_step.get("selector"))
            value = _clean_text(normalized_step.get("value"))
            if not selector or not value:
                continue
            normalized_step["selector"] = selector
            normalized_step["value"] = value

        if step_type == "verify_text":
            raw_value = _clean_text(normalized_step.get("value")).lower()
            if raw_value in {"example", "example domain"} and has_explicit_start_url and not is_example_site:
                continue
            selector = _to_text_wait_selector(normalized_step.get("selector"))
            if selector.lower() in {"body", "html", "main", "h1", "h2", "h3"}:
                value_text = _clean_text(normalized_step.get("value"))
                if value_text:
                    selector = f"text={value_text}"
            if not selector:
                continue
            normalized_step["selector"] = selector

        if step_type == "wait":
            until = _clean_text(normalized_step.get("until")).lower() or "timeout"
            if until not in {"timeout", "selector_visible", "selector_hidden", "load_state"}:
                until = "timeout"
            normalized_step["until"] = until
            if until in {"selector_visible", "selector_hidden"}:
                selector = _to_text_wait_selector(normalized_step.get("selector"))
                if not selector:
                    continue
                normalized_step["selector"] = selector

        if step_type == "drag":
            source_selector = _to_text_wait_selector(normalized_step.get("source_selector"))
            target_selector = _to_text_wait_selector(normalized_step.get("target_selector"))
            if not source_selector or not target_selector:
                continue
            normalized_step["source_selector"] = source_selector
            normalized_step["target_selector"] = target_selector

        token = json.dumps(normalized_step, sort_keys=True, ensure_ascii=False)
        if token in seen_step_tokens:
            continue
        seen_step_tokens.add(token)
        sanitized.append(normalized_step)

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
    auto_drag_pre_click_enabled: bool = True,
    auto_drag_post_wait_ms: int = 120,
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

        if auto_drag_pre_click_enabled and not prev_is_same_click and len(expanded) < max_steps:
            expanded.append({"type": "click", "selector": source_selector})

        if len(expanded) < max_steps:
            expanded.append(dict(step))

        if auto_drag_post_wait_ms > 0 and len(expanded) < max_steps:
            expanded.append({"type": "wait", "until": "timeout", "ms": auto_drag_post_wait_ms})

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

    if settings.auth_enabled:
        init_auth_database()
        app.include_router(auth_router)

    run_store = build_run_store(settings)
    suite_store = build_suite_store(settings)
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
    suite_executor = SuiteExecutor(
        settings,
        run_store,
        suite_store,
        test_case_store,
        executor,
        file_client,
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
            "jwt_auth_enabled": settings.auth_enabled,
        }

    @app.post("/api/runs", response_model=RunState)
    async def create_run(
        request: RunCreateRequest,
        background_tasks: BackgroundTasks,
        _: None = Depends(require_admin_auth),
    ) -> RunState:
        raw_steps = [step.model_dump(exclude_none=True) for step in request.steps]
        raw_steps = _sanitize_plan_steps(raw_steps, start_url=request.start_url)
        expanded_steps = _expand_drag_steps(
            raw_steps,
            max_steps=settings.max_steps_per_run,
            auto_drag_pre_click_enabled=settings.auto_drag_pre_click_enabled,
            auto_drag_post_wait_ms=settings.auto_drag_post_wait_ms,
        )
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
        if request.parent_folder_id and not test_case_store.get_folder(request.parent_folder_id):
            raise HTTPException(status_code=404, detail="Parent folder not found")
        if len(request.steps) > settings.max_steps_per_run:
            raise HTTPException(
                status_code=400,
                detail=f"Step count exceeds max_steps_per_run={settings.max_steps_per_run}",
            )
        sanitized_steps = _sanitize_plan_steps(
            [step.model_dump(exclude_none=True) for step in request.steps],
            start_url=request.start_url,
        )
        validated_request = TestCaseCreateRequest.model_validate(
            {
                "name": request.name,
                "description": request.description,
                "prompt": request.prompt,
                "parent_folder_id": request.parent_folder_id,
                "start_url": request.start_url,
                "steps": sanitized_steps,
                "test_data": request.test_data,
                "selector_profile": request.selector_profile,
            }
        )
        test_case = test_case_store.create(validated_request)
        return test_case

    @app.post("/api/test-folders", response_model=FolderState)
    async def create_test_folder(
        request: FolderCreateRequest,
        _: None = Depends(require_admin_auth),
    ) -> FolderState:
        if request.parent_folder_id and not test_case_store.get_folder(request.parent_folder_id):
            raise HTTPException(status_code=404, detail="Parent folder not found")
        folder = test_case_store.create_folder(request)
        return folder

    @app.get("/api/test-folders", response_model=FolderListResponse)
    async def list_test_folders() -> FolderListResponse:
        return FolderListResponse(items=test_case_store.list_folders())

    @app.delete("/api/test-folders/{folder_id}", status_code=204)
    async def delete_test_folder(
        folder_id: str,
        _: None = Depends(require_admin_auth),
    ) -> None:
        folders = test_case_store.list_folders()
        folder_by_id = {folder.folder_id: folder for folder in folders}
        if folder_id not in folder_by_id:
            raise HTTPException(status_code=404, detail="Folder not found")

        child_map: dict[str, list[str]] = {}
        for folder in folders:
            parent_id = (folder.parent_folder_id or "").strip()
            if not parent_id:
                continue
            current = child_map.get(parent_id) or []
            current.append(folder.folder_id)
            child_map[parent_id] = current

        folder_ids_to_delete: list[str] = []
        stack = [folder_id]
        seen: set[str] = set()
        while stack:
            current_id = stack.pop()
            if current_id in seen:
                continue
            seen.add(current_id)
            folder_ids_to_delete.append(current_id)
            for child_id in child_map.get(current_id, []):
                stack.append(child_id)

        test_cases = test_case_store.list()
        for test_case in test_cases:
            parent_id = (test_case.parent_folder_id or "").strip()
            if parent_id in seen:
                test_case_store.delete(test_case.test_case_id)

        for target_id in reversed(folder_ids_to_delete):
            test_case_store.delete_folder(target_id)

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
            default_wait_ms=settings.planner_default_wait_ms,
        )
        normalized_steps = _sanitize_plan_steps(normalized_steps, start_url=start_url)
        normalized_steps = _expand_drag_steps(
            normalized_steps,
            max_steps=settings.max_steps_per_run,
            auto_drag_pre_click_enabled=settings.auto_drag_pre_click_enabled,
            auto_drag_post_wait_ms=settings.auto_drag_post_wait_ms,
        )
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

    @app.put("/api/test-cases/{test_case_id}", response_model=TestCaseState)
    async def update_test_case(
        test_case_id: str,
        request: TestCaseUpdateRequest,
        _: None = Depends(require_admin_auth),
    ) -> TestCaseState:
        current = test_case_store.get(test_case_id)
        if not current:
            raise HTTPException(status_code=404, detail="Test case not found")
        if request.parent_folder_id and not test_case_store.get_folder(request.parent_folder_id):
            raise HTTPException(status_code=404, detail="Parent folder not found")
        if len(request.steps) > settings.max_steps_per_run:
            raise HTTPException(
                status_code=400,
                detail=f"Step count exceeds max_steps_per_run={settings.max_steps_per_run}",
            )

        sanitized_steps = _sanitize_plan_steps(
            [step.model_dump(exclude_none=True) for step in request.steps],
            start_url=request.start_url,
        )
        validated_request = TestCaseUpdateRequest.model_validate(
            {
                "name": request.name,
                "description": request.description,
                "prompt": request.prompt,
                "parent_folder_id": request.parent_folder_id,
                "start_url": request.start_url,
                "steps": sanitized_steps,
                "test_data": request.test_data,
                "selector_profile": request.selector_profile,
            }
        )

        current.name = validated_request.name
        current.description = validated_request.description
        current.prompt = validated_request.prompt
        current.parent_folder_id = validated_request.parent_folder_id
        current.start_url = validated_request.start_url
        current.steps = validated_request.steps
        current.test_data = validated_request.test_data
        current.selector_profile = validated_request.selector_profile
        test_case_store.persist(current)
        return current

    @app.delete("/api/test-cases/{test_case_id}", status_code=204)
    async def delete_test_case(
        test_case_id: str,
        _: None = Depends(require_admin_auth),
    ) -> None:
        deleted = test_case_store.delete(test_case_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Test case not found")

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
        sanitized_steps = _sanitize_plan_steps(
            [step.model_dump(exclude_none=True) for step in run_request.steps],
            start_url=run_request.start_url,
        )
        expanded_steps = _expand_drag_steps(
            sanitized_steps,
            max_steps=settings.max_steps_per_run,
            auto_drag_pre_click_enabled=settings.auto_drag_pre_click_enabled,
            auto_drag_post_wait_ms=settings.auto_drag_post_wait_ms,
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
        parsed_steps = parse_structured_task_steps(
            request.task,
            max_steps=max_steps,
            auto_login_wait_ms=settings.auto_login_wait_ms,
            auto_create_confirm_wait_ms=settings.auto_create_confirm_wait_ms,
            default_wait_ms=settings.default_wait_ms,
            structured_selector_wait_ms=settings.structured_selector_wait_ms,
            structured_options_wait_ms=settings.structured_options_wait_ms,
        )
        if parsed_steps:
            parsed_steps = _sanitize_plan_steps(parsed_steps, start_url=None)
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
            normalized_steps = normalize_plan_steps(
                payload.get("steps"),
                max_steps=max_steps,
                default_wait_ms=settings.planner_default_wait_ms,
            )
            normalized_steps = _sanitize_plan_steps(normalized_steps, start_url=payload.get("start_url"))
            normalized_steps = _ensure_drag_step(request.task, normalized_steps)
            normalized_steps = _expand_drag_steps(
                normalized_steps,
                max_steps=max_steps,
                auto_drag_pre_click_enabled=settings.auto_drag_pre_click_enabled,
                auto_drag_post_wait_ms=settings.auto_drag_post_wait_ms,
            )
            if not normalized_steps:
                normalized_steps = build_recovery_steps(
                    request.task,
                    max_steps=max_steps,
                    load_state_wait_ms=settings.recovery_load_state_wait_ms,
                    timeout_wait_ms=settings.planner_default_wait_ms,
                )
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

    @app.post("/api/suite-runs", response_model=SuiteRunState)
    async def create_suite_run(
        request: SuiteRunCreateRequest,
        background_tasks: BackgroundTasks,
        _: None = Depends(require_admin_auth),
    ) -> SuiteRunState:
        all_test_cases = test_case_store.list()
        test_by_id = {item.test_case_id: item for item in all_test_cases}

        target_ids: list[str] = list(request.test_case_ids)
        if request.folder_id:
            all_folders = test_case_store.list_folders()
            folder_by_id = {item.folder_id: item for item in all_folders}
            if request.folder_id not in folder_by_id:
                raise HTTPException(status_code=404, detail="Folder not found")

            child_map: dict[str, list[str]] = {}
            for folder in all_folders:
                parent = (folder.parent_folder_id or "").strip()
                if not parent:
                    continue
                current = child_map.get(parent) or []
                current.append(folder.folder_id)
                child_map[parent] = current

            stack = [request.folder_id]
            folder_scope: set[str] = set()
            while stack:
                current = stack.pop()
                if current in folder_scope:
                    continue
                folder_scope.add(current)
                for child_id in child_map.get(current, []):
                    stack.append(child_id)

            folder_test_ids = [
                item.test_case_id
                for item in all_test_cases
                if (item.parent_folder_id or "").strip() in folder_scope
            ]
            for case_id in folder_test_ids:
                if case_id not in target_ids:
                    target_ids.append(case_id)

        if not target_ids:
            raise HTTPException(status_code=422, detail="No test cases selected for suite run")

        selected_test_cases: list[TestCaseState] = []
        missing_ids: list[str] = []
        for case_id in target_ids:
            detail = test_case_store.get(case_id)
            if not detail:
                missing_ids.append(case_id)
                continue
            selected_test_cases.append(detail)

        if missing_ids:
            raise HTTPException(status_code=404, detail=f"Test case(s) not found: {', '.join(missing_ids)}")
        if not selected_test_cases:
            raise HTTPException(status_code=422, detail="No test cases resolved for suite run")

        suite_run = suite_store.create(
            SuiteRunCreateRequest.model_validate(
                {
                    "suite_name": request.suite_name,
                    "folder_id": request.folder_id,
                    "test_case_ids": [item.test_case_id for item in selected_test_cases],
                    "max_parallel": request.max_parallel,
                }
            ),
            selected_test_cases,
        )
        background_tasks.add_task(suite_executor.execute, suite_run.suite_run_id)
        LOGGER.info("Suite run created: %s", suite_run.suite_run_id)
        return suite_run

    @app.get("/api/suite-runs", response_model=SuiteRunListResponse)
    async def list_suite_runs() -> SuiteRunListResponse:
        return SuiteRunListResponse(items=suite_store.list())

    @app.get("/api/suite-runs/{suite_run_id}", response_model=SuiteRunState)
    async def get_suite_run(suite_run_id: str) -> SuiteRunState:
        suite_run = suite_store.get(suite_run_id)
        if not suite_run:
            raise HTTPException(status_code=404, detail="Suite run not found")
        return suite_run

    @app.get("/api/suite-runs/{suite_run_id}/artifacts/{artifact_name:path}")
    async def get_suite_artifact(suite_run_id: str, artifact_name: str) -> FileResponse:
        suite_run = suite_store.get(suite_run_id)
        if not suite_run:
            raise HTTPException(status_code=404, detail="Suite run not found")
        run_dir = (settings.artifact_root / suite_run_id).resolve()
        artifact_path = (run_dir / artifact_name).resolve()
        if not artifact_path.is_relative_to(run_dir):
            raise HTTPException(status_code=400, detail="Invalid artifact path")
        if not artifact_path.exists() or not artifact_path.is_file():
            raise HTTPException(status_code=404, detail="Artifact not found")
        return FileResponse(artifact_path)

    @app.post("/api/suite-runs/{suite_run_id}/cancel", response_model=CancelSuiteRunResponse)
    async def cancel_suite_run(
        suite_run_id: str,
        _: None = Depends(require_admin_auth),
    ) -> CancelSuiteRunResponse:
        suite_run = suite_store.mark_cancelled(suite_run_id)
        if not suite_run:
            raise HTTPException(status_code=404, detail="Suite run not found")
        return CancelSuiteRunResponse(suite_run_id=suite_run_id, status=suite_run.status)

    @app.get("/api/runs", response_model=RunListResponse)
    async def list_runs() -> RunListResponse:
        return RunListResponse(items=run_store.list())

    @app.get("/api/runs/{run_id}", response_model=RunState)
    async def get_run(run_id: str) -> RunState:
        run = run_store.get(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        return run

    @app.post("/api/runs/{run_id}/recover-selector", response_model=RunState)
    async def recover_run_selector(
        run_id: str,
        request: SelectorRecoveryRequest,
        background_tasks: BackgroundTasks,
        _: None = Depends(require_admin_auth),
    ) -> RunState:
        run = run_store.get(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        if request.step_index >= len(run.steps):
            raise HTTPException(status_code=400, detail="step_index is out of range")

        raw_steps = [dict(step.input or {}) for step in run.steps]
        target_step = dict(raw_steps[request.step_index] or {})
        if not target_step:
            raise HTTPException(status_code=400, detail="Target step has no editable input payload")

        target_step["type"] = str(target_step.get("type") or run.steps[request.step_index].type)
        target_step[request.field] = request.selector
        raw_steps[request.step_index] = target_step

        resume_steps = raw_steps[request.step_index:]
        run_name = request.run_name or f"{run.run_name} [resume-step-{request.step_index + 1}]"
        sanitized_steps = _sanitize_plan_steps(resume_steps, start_url=run.start_url)
        expanded_steps = _expand_drag_steps(
            sanitized_steps,
            max_steps=settings.max_steps_per_run,
            auto_drag_pre_click_enabled=settings.auto_drag_pre_click_enabled,
            auto_drag_post_wait_ms=settings.auto_drag_post_wait_ms,
        )
        run_request = RunCreateRequest.model_validate(
            {
                "run_name": run_name,
                "start_url": run.start_url,
                "steps": expanded_steps,
                "test_data": run.test_data,
                "selector_profile": run.selector_profile,
            }
        )

        recovered = run_store.create(run_request)
        background_tasks.add_task(executor.execute, recovered.run_id)
        LOGGER.info(
            "Run selector recovery started (resume): new_run_id=%s source_run_id=%s step_index=%s field=%s",
            recovered.run_id,
            run_id,
            request.step_index,
            request.field,
        )
        return recovered

    @app.get("/api/runs/{run_id}/artifacts/{artifact_name:path}")
    async def get_run_artifact(run_id: str, artifact_name: str) -> FileResponse:
        run = run_store.get(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        run_dir = (settings.artifact_root / run_id).resolve()
        artifact_path = (run_dir / artifact_name).resolve()
        if not artifact_path.is_relative_to(run_dir):
            raise HTTPException(status_code=400, detail="Invalid artifact path")
        if not artifact_path.exists() or not artifact_path.is_file():
            raise HTTPException(status_code=404, detail="Artifact not found")

        return FileResponse(artifact_path)

    @app.post("/api/runs/{run_id}/cancel", response_model=CancelRunResponse)
    async def cancel_run(run_id: str, _: None = Depends(require_admin_auth)) -> CancelRunResponse:
        run = run_store.mark_cancelled(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        return CancelRunResponse(run_id=run_id, status=run.status)

    return app


app = build_app()
