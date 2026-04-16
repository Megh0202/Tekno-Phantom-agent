from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
from html import escape
import json
import logging
import re
import sys
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Header, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.auth.dependencies import build_api_auth_dependency
from app.brain.http_client import HttpBrainClient
from app.config import Settings, get_settings
from app.database import init_auth_database
from app.mcp.browser_client import build_browser_client
from app.mcp.filesystem_client import build_filesystem_client
from app.models.user import User
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
from app.runtime.viewer_session import ViewerSessionManager
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
    RunResumeRequest,
    RunState,
    RunStatus,
    SuiteRunCreateRequest,
    SuiteRunListResponse,
    SuiteRunState,
    StepImportResponse,
    StepSelectorHelpRequest,
    StepStatus,
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

    def _normalize_target(raw: object) -> dict[str, str] | None:
        if not isinstance(raw, dict):
            return None
        allowed_fields = ("kind", "role", "text", "label", "placeholder", "context")
        normalized: dict[str, str] = {}
        for field in allowed_fields:
            value = _clean_text(raw.get(field))
            if value:
                normalized[field] = value
        return normalized or None

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
        target = _normalize_target(normalized_step.get("target"))
        if target is not None:
            normalized_step["target"] = target
        else:
            normalized_step.pop("target", None)

        if step_type in {"click", "type", "select", "verify_text", "wait", "handle_popup"}:
            if "selector" in normalized_step:
                normalized_step["selector"] = _normalize_selector(normalized_step.get("selector"))

        if step_type == "click":
            selector = _to_click_selector(normalized_step.get("selector"))
            if not selector and target is None:
                continue
            if selector.lower() in generic_click_targets:
                continue
            normalized_step["selector"] = selector

        if step_type == "type":
            selector = _to_text_wait_selector(normalized_step.get("selector"))
            text_value = _clean_text(normalized_step.get("text"))
            if (not selector and target is None) or not text_value:
                continue
            normalized_step["selector"] = selector
            normalized_step["text"] = text_value
            normalized_step["clear_first"] = bool(normalized_step.get("clear_first", True))

        if step_type == "select":
            selector = _to_text_wait_selector(normalized_step.get("selector"))
            value = _clean_text(normalized_step.get("value"))
            if (not selector and target is None) or not value:
                continue
            normalized_step["selector"] = selector
            normalized_step["value"] = value

        if step_type == "verify_text":
            raw_value = _clean_text(normalized_step.get("value")).lower()
            if raw_value in {"example", "example domain"} and has_explicit_start_url and not is_example_site:
                continue
            selector = _to_text_wait_selector(normalized_step.get("selector"))
            if not is_example_site and selector.lower() in {"body", "html", "main", "h1", "h2", "h3"}:
                value_text = _clean_text(normalized_step.get("value"))
                if value_text:
                    selector = f"text={value_text}"
            if not selector and target is None:
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


def _resolve_start_url(
    start_url: str | None,
    steps: list[dict[str, object]],
) -> str | None:
    normalized = (start_url or "").strip()
    if normalized:
        return normalized

    for step in steps:
        step_type = str(step.get("type") or "").strip().lower()
        if step_type != "navigate":
            continue
        candidate = str(step.get("url") or "").strip()
        if candidate:
            return candidate
    return None


def _has_navigate_step(steps: list[dict[str, object]]) -> bool:
    return any(str(step.get("type") or "").strip().lower() == "navigate" for step in steps)


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

    known_field_source_aliases = {
        "short_answer_source",
        "{{selector.short_answer_source}}",
        "email_field_source",
        "{{selector.email_field_source}}",
        "dropdown_field_source",
        "{{selector.dropdown_field_source}}",
    }

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

        skip_pre_click_for_alias = source_selector.strip().lower() in known_field_source_aliases

        if (
            auto_drag_pre_click_enabled
            and not skip_pre_click_for_alias
            and not prev_is_same_click
            and len(expanded) < max_steps
        ):
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


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


def _plan_trace_dir(settings: Settings) -> Path:
    trace_dir = settings.artifact_root / f"plan-{_utc_stamp()}"
    trace_dir.mkdir(parents=True, exist_ok=True)
    return trace_dir


def _write_plan_trace(trace_dir: Path, trace: dict[str, object]) -> None:
    (trace_dir / "plan-trace.json").write_text(
        json.dumps(trace, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _actor_user_id(actor: User | None) -> int:
    return int(actor.id) if actor is not None else 1


def _run_owner_id(actor: User | None) -> int:
    return int(actor.id) if actor is not None else 0


def _viewer_supported(settings: Settings) -> bool:
    return settings.browser_mode == "playwright" and settings.browser_viewer_enabled and not settings.playwright_headless


def _can_access_owned_resource(actor: User | None, owner_id: int) -> bool:
    return actor is None or int(actor.id) == int(owner_id)


def _is_enterprise_prompt(task: str) -> bool:
    lowered = task.lower()
    return any(
        token in lowered
        for token in (
            "vita",
            "workflow",
            "workflows",
            "form canvas",
            "short answer",
            "status",
            "transition",
            "editor",
        )
    )


def _validate_generated_plan(task: str, normalized_steps: list[dict[str, object]]) -> dict[str, object]:
    errors: list[str] = []
    missing_steps: list[str] = []
    rejection_reasons: list[str] = []
    lowered_task = task.lower()
    step_types = [str(step.get("type") or "").strip().lower() for step in normalized_steps]
    is_enterprise = _is_enterprise_prompt(task)

    if not normalized_steps:
        errors.append("Planner response was dropped during normalization or produced no runnable steps.")
        rejection_reasons.append("Normalized plan is empty after dropping invalid steps.")

    if "wait" in lowered_task and "wait" not in step_types:
        errors.append("Prompt requested a wait step, but no wait step was generated.")
        missing_steps.append("wait step")
        rejection_reasons.append("Missing required wait step from the user prompt.")

    if "verify" in lowered_task and not any(step_type.startswith("verify_") for step_type in step_types):
        errors.append("Prompt requested verify/verification behavior, but no verify step was generated.")
        missing_steps.append("verify step")
        rejection_reasons.append("Missing requested verification step.")

    if ("login" in lowered_task or "sign in" in lowered_task) and "password" in lowered_task:
        if not any(
            step_type == "type" and "password" in str(step.get("selector") or "").lower()
            for step, step_type in zip(normalized_steps, step_types)
        ):
            missing_steps.append("password entry")
            rejection_reasons.append("Missing password entry for login workflow.")
        if not any(
            step_type == "click" and any(token in str(step.get("selector") or "").lower() for token in ("login", "sign in"))
            for step, step_type in zip(normalized_steps, step_types)
        ):
            missing_steps.append("login submit click")
            rejection_reasons.append("Missing login submit click.")

    if "drag" in lowered_task and "drag" not in step_types:
        missing_steps.append("drag step")
        rejection_reasons.append("Missing drag step for drag-and-drop workflow.")

    if any(token in lowered_task for token in ("form name", "enter form name")) and "type" not in step_types:
        missing_steps.append("form fill step")
        rejection_reasons.append("Missing form fill step for the requested workflow.")

    if any(token in lowered_task for token in ("verify", "is visible", "visible in the editor")):
        verify_steps = [step for step, step_type in zip(normalized_steps, step_types) if step_type == "verify_text"]
        if not verify_steps:
            missing_steps.append("specific verification target")
            rejection_reasons.append("Missing verification step with a specific target.")
        else:
            has_specific_target = any(
                str(step.get("selector") or "").strip()
                and str(step.get("selector") or "").strip().lower() not in {"h1", "body"}
                for step in verify_steps
            )
            if not has_specific_target:
                missing_steps.append("specific verification target")
                rejection_reasons.append("Verification target is too generic for the requested check.")

    if any(token in lowered_task for token in ("extract", "return the details", "return details")):
        errors.append("Extraction/output requests are not supported by the current planner contract.")
        rejection_reasons.append("Unsupported extraction/output request in prompt.")

    return {
        "valid": not errors and not missing_steps and not rejection_reasons,
        "errors": errors,
        "missing_steps": missing_steps,
        "rejection_reasons": rejection_reasons,
        "is_enterprise_prompt": is_enterprise,
    }


def _build_structured_plan_attempt(
    request: PlanGenerateRequest,
    *,
    max_steps: int,
    settings: Settings,
) -> dict[str, object] | None:
    parsed_steps = parse_structured_task_steps(
        request.task,
        max_steps=max_steps,
        auto_login_wait_ms=settings.auto_login_wait_ms,
        auto_create_confirm_wait_ms=settings.auto_create_confirm_wait_ms,
        default_wait_ms=settings.default_wait_ms,
        structured_selector_wait_ms=settings.structured_selector_wait_ms,
        structured_options_wait_ms=settings.structured_options_wait_ms,
    )
    if not parsed_steps:
        return None

    normalized_steps = _sanitize_plan_steps(parsed_steps, start_url=request.start_url)
    normalized_steps = _ensure_drag_step(request.task, normalized_steps)
    validation = _validate_generated_plan(request.task, normalized_steps)
    return {
        "source": "structured_parser",
        "planning_task": request.task,
        "payload": {
            "run_name": "prompt-steps-run",
            "start_url": request.start_url,
            "steps": parsed_steps,
        },
        "normalized_steps": normalized_steps,
        "validation": validation,
        "run_name": "prompt-steps-run",
        "start_url": request.start_url,
    }


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
    viewer_sessions = ViewerSessionManager(settings) if _viewer_supported(settings) else None
    browser_client = build_browser_client(settings, viewer_sessions=viewer_sessions)
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
        viewer_sessions=viewer_sessions,
    )
    require_admin_auth = build_admin_auth_dependency(settings)
    require_api_access = build_api_auth_dependency(settings)
    app.state.settings = settings
    app.state.run_store = run_store
    app.state.test_case_store = test_case_store
    app.state.suite_store = suite_store
    app.state.executor = executor
    app.state.suite_executor = suite_executor
    app.state.viewer_sessions = viewer_sessions

    @app.on_event("shutdown")
    async def shutdown_event() -> None:
        if viewer_sessions is not None:
            await viewer_sessions.aclose()
        await file_client.aclose()

    def prepare_run_viewer(run: RunState) -> RunState:
        if viewer_sessions is None:
            run.viewer_token = None
            run.viewer_url = None
            run.viewer_status = None
            run.viewer_last_error = None
            return run
        info = viewer_sessions.prepare_run(run.run_id, token=run.viewer_token)
        if info is None:
            run.viewer_token = None
            run.viewer_url = None
            run.viewer_status = None
            run.viewer_last_error = None
            return run
        run.viewer_token = info.token
        run.viewer_url = info.viewer_url
        run.viewer_status = info.status
        run.viewer_last_error = info.error
        return run

    def validate_viewer_access(run_id: str, token: str | None) -> tuple[RunState, str]:
        run = run_store.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        expected = (run.viewer_token or "").strip()
        provided = (token or "").strip()
        if not expected or provided != expected:
            raise HTTPException(status_code=404, detail="Viewer session not found")
        return run, expected

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
        actor: User | None = Depends(require_api_access),
    ) -> RunState:
        raw_steps = [step.model_dump(exclude_none=True) for step in request.steps]
        resolved_start_url = _resolve_start_url(request.start_url, raw_steps)
        if request.execution_mode == "plan" and not resolved_start_url and not _has_navigate_step(raw_steps):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Run has no entry URL. Provide start_url or include at least one navigate step "
                    "before interaction steps."
                ),
            )
        if len(raw_steps) > settings.max_steps_per_run:
            raise HTTPException(
                status_code=400,
                detail=f"Step count exceeds max_steps_per_run={settings.max_steps_per_run}",
            )
        expanded_request = RunCreateRequest.model_validate(
            {
                "run_name": request.run_name,
                "start_url": resolved_start_url,
                "prompt": request.prompt,
                "execution_mode": request.execution_mode,
                "failure_mode": request.failure_mode,
                "steps": raw_steps,
                "test_data": request.test_data,
                "selector_profile": request.selector_profile,
                "source_test_case_id": request.source_test_case_id,
                "resume_from_step_index": request.resume_from_step_index,
            }
        )

        if len(expanded_request.steps) > settings.max_steps_per_run:
            raise HTTPException(
                status_code=400,
                detail=f"Step count exceeds max_steps_per_run={settings.max_steps_per_run}",
            )

        run = run_store.create(expanded_request, user_id=_run_owner_id(actor))
        prepare_run_viewer(run)
        run_store.persist(run)
        background_tasks.add_task(executor.execute, run.run_id)
        LOGGER.info("Run created: %s", run.run_id)
        return run

    @app.post("/api/test-cases", response_model=TestCaseState)
    async def create_test_case(
        request: TestCaseCreateRequest,
        actor: User | None = Depends(require_api_access),
    ) -> TestCaseState:
        if request.parent_folder_id:
            parent_folder = test_case_store.get_folder(request.parent_folder_id)
            if not parent_folder or not _can_access_owned_resource(actor, parent_folder.user_id):
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
        resolved_start_url = _resolve_start_url(request.start_url, sanitized_steps)
        validated_request = TestCaseCreateRequest.model_validate(
            {
                "name": request.name,
                "description": request.description,
                "prompt": request.prompt,
                "parent_folder_id": request.parent_folder_id,
                "start_url": resolved_start_url,
                "steps": sanitized_steps,
                "test_data": request.test_data,
                "selector_profile": request.selector_profile,
            }
        )
        test_case = test_case_store.create(validated_request, user_id=_actor_user_id(actor))
        return test_case

    @app.post("/api/test-folders", response_model=FolderState)
    async def create_test_folder(
        request: FolderCreateRequest,
        actor: User | None = Depends(require_api_access),
    ) -> FolderState:
        if request.parent_folder_id:
            parent_folder = test_case_store.get_folder(request.parent_folder_id)
            if not parent_folder or not _can_access_owned_resource(actor, parent_folder.user_id):
                raise HTTPException(status_code=404, detail="Parent folder not found")
        folder = test_case_store.create_folder(request, user_id=_actor_user_id(actor))
        return folder

    @app.get("/api/test-folders", response_model=FolderListResponse)
    async def list_test_folders(actor: User | None = Depends(require_api_access)) -> FolderListResponse:
        folders = [item for item in test_case_store.list_folders() if _can_access_owned_resource(actor, item.user_id)]
        return FolderListResponse(items=folders)

    @app.delete("/api/test-folders/{folder_id}", status_code=204)
    async def delete_test_folder(
        folder_id: str,
        actor: User | None = Depends(require_api_access),
    ) -> None:
        folders = test_case_store.list_folders()
        folder_by_id = {folder.folder_id: folder for folder in folders}
        target_folder = folder_by_id.get(folder_id)
        if target_folder is None or not _can_access_owned_resource(actor, target_folder.user_id):
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
            if parent_id in seen and _can_access_owned_resource(actor, test_case.user_id):
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
    async def list_test_cases(actor: User | None = Depends(require_api_access)) -> TestCaseListResponse:
        cases = [item for item in test_case_store.list() if _can_access_owned_resource(actor, item.user_id)]
        return TestCaseListResponse(items=cases)

    @app.get("/api/test-cases/{test_case_id}", response_model=TestCaseState)
    async def get_test_case(
        test_case_id: str,
        actor: User | None = Depends(require_api_access),
    ) -> TestCaseState:
        test_case = test_case_store.get(test_case_id)
        if not test_case or not _can_access_owned_resource(actor, test_case.user_id):
            raise HTTPException(status_code=404, detail="Test case not found")
        return test_case

    @app.put("/api/test-cases/{test_case_id}", response_model=TestCaseState)
    async def update_test_case(
        test_case_id: str,
        request: TestCaseUpdateRequest,
        actor: User | None = Depends(require_api_access),
    ) -> TestCaseState:
        current = test_case_store.get(test_case_id)
        if not current or not _can_access_owned_resource(actor, current.user_id):
            raise HTTPException(status_code=404, detail="Test case not found")
        if request.parent_folder_id:
            parent_folder = test_case_store.get_folder(request.parent_folder_id)
            if not parent_folder or not _can_access_owned_resource(actor, parent_folder.user_id):
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
        resolved_start_url = _resolve_start_url(request.start_url, sanitized_steps)
        validated_request = TestCaseUpdateRequest.model_validate(
            {
                "name": request.name,
                "description": request.description,
                "prompt": request.prompt,
                "parent_folder_id": request.parent_folder_id,
                "start_url": resolved_start_url,
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
        actor: User | None = Depends(require_api_access),
    ) -> None:
        existing = test_case_store.get(test_case_id)
        if not existing or not _can_access_owned_resource(actor, existing.user_id):
            raise HTTPException(status_code=404, detail="Test case not found")
        deleted = test_case_store.delete(test_case_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Test case not found")

    @app.post("/api/test-cases/{test_case_id}/run", response_model=RunState)
    async def run_test_case(
        test_case_id: str,
        background_tasks: BackgroundTasks,
        actor: User | None = Depends(require_api_access),
    ) -> RunState:
        test_case = test_case_store.get(test_case_id)
        if not test_case or not _can_access_owned_resource(actor, test_case.user_id):
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
        expanded_run_request = RunCreateRequest.model_validate(
            {
                "run_name": run_request.run_name,
                "start_url": run_request.start_url,
                "steps": [step.model_dump(exclude_none=True) for step in run_request.steps],
                "test_data": run_request.test_data,
                "selector_profile": run_request.selector_profile,
            }
        )
        run = run_store.create(expanded_run_request, user_id=int(test_case.user_id))
        prepare_run_viewer(run)
        run_store.persist(run)
        background_tasks.add_task(executor.execute, run.run_id)
        LOGGER.info("Run created from test case: %s (test_case_id=%s)", run.run_id, test_case_id)
        return run

    @app.post("/api/runs/{run_id}/steps/{step_id}/selector", response_model=RunState)
    async def submit_step_selector(
        run_id: str,
        step_id: str,
        request: StepSelectorHelpRequest,
        actor: User | None = Depends(require_api_access),
    ) -> RunState:
        existing = run_store.get(run_id)
        if existing is None or not _can_access_owned_resource(actor, existing.user_id):
            raise HTTPException(status_code=404, detail="Run or step not found")
        try:
            run = executor.apply_manual_selector_hint(run_id, step_id, request.selector)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if run is None:
            raise HTTPException(status_code=404, detail="Run or step not found")
        await executor.execute(run.run_id)
        refreshed = run_store.get(run.run_id)
        if refreshed is None:
            raise HTTPException(status_code=404, detail="Run not found after selector retry")

        # Persist the working user-provided selector to the test case's selector_profile
        # so future runs automatically use it without requiring manual input.
        succeeded_step = next((s for s in refreshed.steps if s.step_id == step_id), None)
        if (
            succeeded_step is not None
            and succeeded_step.status == StepStatus.completed
            and succeeded_step.provided_selector
            and refreshed.source_test_case_id
        ):
            recovery_key = succeeded_step.input.get("_recovery_selector_key") if succeeded_step.input else None
            if isinstance(recovery_key, str) and recovery_key.strip():
                # Extract profile key if selector was a template like {{selector.KEY}}
                template_match = re.fullmatch(r"\{\{\s*selector\.([a-zA-Z0-9_.-]+)\s*\}\}", recovery_key.strip())
                profile_key = template_match.group(1) if template_match else None

                if not profile_key:
                    # For non-template selectors, normalise the raw selector text into a key
                    profile_key = re.sub(r"[^a-z0-9_]", "_", recovery_key.strip().lower())[:64].strip("_") or None

                if profile_key:
                    test_case = test_case_store.get(refreshed.source_test_case_id)
                    if test_case is not None:
                        updated_profile = dict(test_case.selector_profile)
                        existing = list(updated_profile.get(profile_key, []))
                        new_selector = succeeded_step.provided_selector
                        if new_selector not in existing:
                            updated_profile[profile_key] = [new_selector] + existing
                            test_case.selector_profile = updated_profile
                            test_case_store.persist(test_case)
                            LOGGER.info(
                                "Persisted user selector %r to test case %s selector_profile[%r]",
                                new_selector,
                                refreshed.source_test_case_id,
                                profile_key,
                            )

        return refreshed

    @app.post("/api/plan", response_model=PlanGenerateResponse)
    async def generate_plan(
        request: PlanGenerateRequest,
        _: None = Depends(require_admin_auth),
    ) -> PlanGenerateResponse:
        max_steps = request.max_steps or settings.max_steps_per_run
        max_steps = min(max_steps, settings.max_steps_per_run)
        trace_dir = _plan_trace_dir(settings)
        trace: dict[str, object] = {
            "task": request.task,
            "attempts": [],
            "structured_attempt": None,
            "normalized_plan": None,
            "validation": None,
        }

        structured_attempt = _build_structured_plan_attempt(
            request,
            max_steps=max_steps,
            settings=settings,
        )
        if structured_attempt is not None:
            trace["structured_attempt"] = structured_attempt
            structured_validation = structured_attempt["validation"]
            if bool(structured_validation.get("valid")):
                try:
                    validated = RunCreateRequest.model_validate(
                        {
                            "run_name": structured_attempt["run_name"],
                            "start_url": structured_attempt["start_url"],
                            "steps": structured_attempt["normalized_steps"],
                            "test_data": request.test_data,
                            "selector_profile": request.selector_profile,
                        }
                    )
                except Exception as exc:
                    raise HTTPException(status_code=502, detail=f"Invalid structured plan: {exc}") from exc

                trace["normalized_plan"] = [step.model_dump(exclude_none=True) for step in validated.steps]
                trace["validation"] = structured_validation
                _write_plan_trace(trace_dir, trace)
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

        last_validation: dict[str, object] | None = None
        for attempt_index in range(2):
            attempt_task = planning_task
            if attempt_index == 0 and structured_attempt is not None:
                last_validation = structured_attempt["validation"]
            if attempt_index == 1 and last_validation is not None:
                feedback = " ".join(
                    list(last_validation.get("errors", []))
                    + list(last_validation.get("missing_steps", []))
                    + list(last_validation.get("rejection_reasons", []))
                ).strip()
                attempt_task = (
                    f"{planning_task}\n\n"
                    "Validation feedback from the previous rejected plan:\n"
                    f"{feedback or 'The previous plan did not satisfy the prompt requirements.'}"
                )
                if bool(last_validation.get("is_enterprise_prompt")):
                    attempt_task = (
                        f"{attempt_task}\n\n"
                        "Enterprise planning requirements:\n"
                        "- Cover all requested workflow actions.\n"
                        "- Include required data entry and a specific verification target.\n"
                    )

            try:
                payload = await brain_client.plan_task(attempt_task, max_steps=max_steps)
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
                validation = _validate_generated_plan(request.task, normalized_steps)
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"Invalid plan returned by brain: {exc}") from exc

            trace["attempts"].append(
                {
                    "source": "llm_planner",
                    "planning_task": attempt_task,
                    "payload": payload,
                    "normalized_steps": normalized_steps,
                    "validation": validation,
                }
            )
            last_validation = validation

            if not validation["valid"]:
                continue

            try:
                validated = RunCreateRequest.model_validate(
                    {
                        "run_name": payload.get("run_name", "ai-generated-run"),
                        "start_url": payload.get("start_url"),
                        "steps": normalized_steps,
                        "test_data": request.test_data,
                        "selector_profile": request.selector_profile,
                    }
                )
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"Invalid plan returned by brain: {exc}") from exc

            trace["normalized_plan"] = [step.model_dump(exclude_none=True) for step in validated.steps]
            trace["validation"] = validation
            _write_plan_trace(trace_dir, trace)
            return PlanGenerateResponse(
                run_name=validated.run_name,
                start_url=validated.start_url,
                steps=validated.steps[:max_steps],
            )

        trace["validation"] = last_validation
        _write_plan_trace(trace_dir, trace)
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Generated plan failed validation after one retry.",
                "validation_errors": list((last_validation or {}).get("errors", []))
                + list((last_validation or {}).get("missing_steps", []))
                + list((last_validation or {}).get("rejection_reasons", [])),
            },
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
    async def list_runs(actor: User | None = Depends(require_api_access)) -> RunListResponse:
        return RunListResponse(
            items=[item for item in run_store.list() if _can_access_owned_resource(actor, item.user_id)]
        )

    @app.get("/api/runs/{run_id}", response_model=RunState)
    async def get_run(run_id: str, actor: User | None = Depends(require_api_access)) -> RunState:
        run = run_store.get(run_id)
        if not run or not _can_access_owned_resource(actor, run.user_id):
            raise HTTPException(status_code=404, detail="Run not found")
        return run

    @app.post("/api/runs/{run_id}/resume", response_model=RunState)
    async def resume_run(
        run_id: str,
        request: RunResumeRequest,
        background_tasks: BackgroundTasks,
        actor: User | None = Depends(require_api_access),
    ) -> RunState:
        run = run_store.get(run_id)
        if not run or not _can_access_owned_resource(actor, run.user_id):
            raise HTTPException(status_code=404, detail="Run not found")

        resume_index = next(
            (
                index
                for index, step in enumerate(run.steps)
                if step.status in {StepStatus.failed, StepStatus.waiting_for_input}
            ),
            None,
        )

        if resume_index is None:
            resume_index = next(
                (
                    index
                    for index, step in enumerate(run.steps)
                    if step.status in {StepStatus.pending, StepStatus.running}
                ),
                None,
            )

        if resume_index is None:
            raise HTTPException(status_code=400, detail="No resumable step found for this run")

        raw_steps = [dict(step.input or {}) for step in run.steps]
        resume_steps = raw_steps[resume_index:]
        resume_start_url = _resolve_start_url(run.start_url, raw_steps)
        sanitized_steps = _sanitize_plan_steps(resume_steps, start_url=run.start_url)
        expanded_steps = _expand_drag_steps(
            sanitized_steps,
            max_steps=settings.max_steps_per_run,
            auto_drag_pre_click_enabled=settings.auto_drag_pre_click_enabled,
            auto_drag_post_wait_ms=settings.auto_drag_post_wait_ms,
        )
        if not expanded_steps:
            raise HTTPException(status_code=400, detail="No runnable steps found for resume")

        resume_name = request.run_name or f"{run.run_name} [resume-step-{resume_index + 1}]"
        resume_request = RunCreateRequest.model_validate(
            {
                "run_name": resume_name,
                "start_url": resume_start_url,
                "steps": expanded_steps,
                "test_data": run.test_data,
                "selector_profile": run.selector_profile,
                "source_test_case_id": run.source_test_case_id,
                "resume_from_step_index": resume_index,
            }
        )

        resumed = run_store.create(resume_request, user_id=run.user_id)
        resumed.viewer_token = run.viewer_token
        prepare_run_viewer(resumed)
        run_store.persist(resumed)
        background_tasks.add_task(executor.execute, resumed.run_id)
        LOGGER.info(
            "Run resume started: new_run_id=%s source_run_id=%s resume_step_index=%s",
            resumed.run_id,
            run_id,
            resume_index,
        )
        return resumed

    @app.post("/api/runs/{run_id}/recover-selector", response_model=RunState)
    async def recover_run_selector(
        run_id: str,
        request: SelectorRecoveryRequest,
        background_tasks: BackgroundTasks,
        actor: User | None = Depends(require_api_access),
    ) -> RunState:
        run = run_store.get(run_id)
        if not run or not _can_access_owned_resource(actor, run.user_id):
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
        resume_start_url = _resolve_start_url(run.start_url, raw_steps)
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
                "start_url": resume_start_url,
                "steps": expanded_steps,
                "test_data": run.test_data,
                "selector_profile": run.selector_profile,
            }
        )

        recovered = run_store.create(run_request, user_id=run.user_id)
        recovered.viewer_token = run.viewer_token
        prepare_run_viewer(recovered)
        run_store.persist(recovered)
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
    async def get_run_artifact(
        run_id: str,
        artifact_name: str,
        actor: User | None = Depends(require_api_access),
    ) -> FileResponse:
        run = run_store.get(run_id)
        if not run or not _can_access_owned_resource(actor, run.user_id):
            raise HTTPException(status_code=404, detail="Run not found")

        run_dir = (settings.artifact_root / run_id).resolve()
        artifact_path = (run_dir / artifact_name).resolve()
        if not artifact_path.is_relative_to(run_dir):
            raise HTTPException(status_code=400, detail="Invalid artifact path")
        if not artifact_path.exists() or not artifact_path.is_file():
            raise HTTPException(status_code=404, detail="Artifact not found")

        return FileResponse(artifact_path)

    @app.post("/api/runs/{run_id}/cancel", response_model=CancelRunResponse)
    async def cancel_run(
        run_id: str,
        actor: User | None = Depends(require_api_access),
    ) -> CancelRunResponse:
        current = run_store.get(run_id)
        if not current or not _can_access_owned_resource(actor, current.user_id):
            raise HTTPException(status_code=404, detail="Run not found")
        run = run_store.mark_cancelled(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        return CancelRunResponse(run_id=run_id, status=run.status)

    @app.get("/viewer/run/{run_id}")
    async def open_run_viewer(run_id: str, token: str) -> HTMLResponse:
        run, validated_token = validate_viewer_access(run_id, token)
        websocket_path = f"/api/runs/{run_id}/viewer/ws?token={quote(validated_token, safe='')}"
        iframe_src = (
            "/viewer/vnc.html"
            f"?autoconnect=1&resize=remote&reconnect=0&path={quote(websocket_path, safe='')}"
        )
        status_url = f"/viewer/run/{run_id}/status?token={quote(validated_token, safe='')}"
        run_name = escape(run.run_name)
        html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Live Browser - {run_name}</title>
    <style>
      * {{ box-sizing: border-box; margin: 0; padding: 0; }}
      html, body {{
        height: 100%;
        background: #000;
        overflow: hidden;
      }}
      iframe {{
        width: 100%;
        height: 100vh;
        border: 0;
        display: block;
        background: #000;
      }}
      .done {{
        display: none;
        height: 100vh;
        place-items: center;
        background: #111;
        color: #f4f4f4;
        font-family: system-ui, -apple-system, sans-serif;
      }}
      .doneCard {{
        width: min(100%, 480px);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 16px;
        background: #1a1a1a;
        padding: 28px;
        text-align: center;
      }}
      .doneCard h1 {{ margin: 0 0 10px; font-size: 24px; }}
      .doneCard p {{ color: #b4b4b4; line-height: 1.55; font-size: 14px; }}
      .doneCard a {{
        display: inline-block;
        margin-top: 18px;
        color: #111;
        background: #ffb300;
        padding: 10px 20px;
        text-decoration: none;
        border-radius: 10px;
        font-weight: 700;
        font-size: 14px;
      }}
    </style>
  </head>
  <body>
    <iframe id="viewer-frame" src="{iframe_src}" title="Live Browser"></iframe>
    <div class="done" id="done">
      <div class="doneCard">
        <h1 id="done-title">Run Finished</h1>
        <p id="done-copy">The live browser session has ended.</p>
        <a href="/">Back to Dashboard</a>
      </div>
    </div>
    <script>
      const statusUrl = {json.dumps(status_url)};
      const viewerFrame = document.getElementById("viewer-frame");
      const done = document.getElementById("done");
      const doneTitle = document.getElementById("done-title");
      const doneCopy = document.getElementById("done-copy");

      function showDone(title, copy) {{
        if (viewerFrame) viewerFrame.style.display = "none";
        if (done) done.style.display = "grid";
        if (doneTitle) doneTitle.textContent = title;
        if (doneCopy) doneCopy.textContent = copy;
      }}

      async function pollStatus() {{
        try {{
          const response = await fetch(statusUrl, {{ cache: "no-store" }});
          if (!response.ok) {{
            showDone("Viewer Ended", "This live browser session is no longer available.");
            return;
          }}
          const payload = await response.json();
          const viewerStatus = payload.viewer_status || "starting";
          if (viewerStatus === "closed") {{
            showDone("Run Finished", "The live browser session has closed. Return to the dashboard to view the report.");
            return;
          }}
          if (viewerStatus === "failed") {{
            showDone("Viewer Stopped", payload.viewer_last_error || "The live browser session stopped unexpectedly.");
            return;
          }}
        }} catch (_error) {{
          showDone("Viewer Ended", "The live browser status could not be refreshed.");
          return;
        }}
        window.setTimeout(pollStatus, 1200);
      }}

      window.setTimeout(pollStatus, 800);
    </script>
  </body>
</html>"""
        return HTMLResponse(html)

    @app.get("/viewer/run/{run_id}/status")
    async def get_run_viewer_status(run_id: str, token: str) -> JSONResponse:
        run, _ = validate_viewer_access(run_id, token)
        return JSONResponse(
            {
                "run_id": run.run_id,
                "run_name": run.run_name,
                "run_status": run.status.value,
                "viewer_status": run.viewer_status,
                "viewer_last_error": run.viewer_last_error,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "viewer_keepalive_seconds": max(int(settings.viewer_keepalive_seconds), 0),
            }
        )

    @app.websocket("/api/runs/{run_id}/viewer/ws")
    async def run_viewer_websocket(websocket: WebSocket, run_id: str) -> None:
        token = websocket.query_params.get("token")
        try:
            validate_viewer_access(run_id, token)
        except HTTPException:
            await websocket.close(code=4404)
            return

        if viewer_sessions is None:
            await websocket.close(code=4404)
            return
        session = viewer_sessions.get_session(run_id)
        if session is None or session.status != "ready":
            await websocket.close(code=1013)
            return

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", session.vnc_port)
        except OSError:
            await websocket.close(code=1011)
            return

        await websocket.accept()

        async def client_to_vnc() -> None:
            try:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        break
                    payload = message.get("bytes")
                    if payload is None:
                        text_payload = message.get("text")
                        if text_payload is None:
                            continue
                        payload = text_payload.encode("utf-8")
                    writer.write(payload)
                    await writer.drain()
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

        async def vnc_to_client() -> None:
            try:
                while True:
                    chunk = await reader.read(65536)
                    if not chunk:
                        break
                    await websocket.send_bytes(chunk)
            finally:
                with contextlib.suppress(Exception):
                    await websocket.close()

        tasks = {
            asyncio.create_task(client_to_vnc()),
            asyncio.create_task(vnc_to_client()),
        }
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            with contextlib.suppress(Exception):
                await task

    app.mount(
        "/viewer",
        StaticFiles(directory=str(settings.viewer_static_root), html=True, check_dir=False),
        name="viewer",
    )

    return app


app = build_app()
