from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from uuid import uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.brain.http_client import HttpBrainClient
from app.auth.dependencies import build_api_auth_dependency, get_current_user
from app.auth.security import issue_csrf_token, set_csrf_cookie
from app.auth.service import ensure_bootstrap_admin
from app.config import Settings, get_settings
from app.database import db_session, init_auth_database
from app.mcp.browser_client import build_browser_client
from app.mcp.filesystem_client import build_filesystem_client
from app.models.user import User
from app.routes.auth import router as auth_router
from app.runtime.executor import AgentExecutor
from app.runtime.instruction_parser import parse_structured_task_steps
from app.runtime.plan_normalizer import extract_verify_text_value, normalize_plan_steps
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
    RunResumeRequest,
    RunState,
    SuiteRunCreateRequest,
    SuiteRunListResponse,
    SuiteRunState,
    StepImportResponse,
    StepSelectorHelpRequest,
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


_STRUCTURED_LINE_RE = re.compile(r"^\s*(?:\d+[\).:-]\s+|[-*]\s+)")
_TASK_URL_RE = re.compile(r"https?://[^\s\"'>]+", flags=re.IGNORECASE)


def _trace_json(payload: object) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def _plan_step_deltas(
    raw_steps: list[object],
    normalized_steps: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    dropped: list[dict[str, object]] = []
    modified: list[dict[str, object]] = []

    normalized_index = 0
    for raw_index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, dict):
            dropped.append({"raw_index": raw_index, "raw_step": raw_step, "reason": "non-dict step dropped"})
            continue

        if normalized_index >= len(normalized_steps):
            dropped.append({"raw_index": raw_index, "raw_step": raw_step, "reason": "dropped during normalization"})
            continue

        normalized_step = normalized_steps[normalized_index]
        normalized_index += 1
        if raw_step != normalized_step:
            modified.append(
                {
                    "raw_index": raw_index,
                    "raw_step": raw_step,
                    "normalized_step": normalized_step,
                }
            )

    return dropped, modified


def _instruction_line_count(task: str) -> int:
    lines = [line.strip() for line in task.splitlines() if line.strip()]
    if not lines:
        return 0
    structured = sum(1 for line in lines if _STRUCTURED_LINE_RE.match(line))
    return structured if structured >= 2 else len(lines)


def _task_contains_any(task: str, needles: tuple[str, ...]) -> bool:
    lowered = task.lower()
    return any(needle in lowered for needle in needles)


def _summarize_step_types(steps: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for step in steps:
        step_type = str(step.get("type") or "").strip().lower()
        if not step_type:
            continue
        counts[step_type] = counts.get(step_type, 0) + 1
    return counts


def _is_enterprise_prompt(task: str) -> bool:
    lowered = task.lower()
    strong_signals = (
        "vita",
        "workflow",
        "form canvas",
        "create form",
        "transition",
        "add status",
        "status editor",
        "form editor",
    )
    if any(signal in lowered for signal in strong_signals):
        return True

    moderate_signals = (
        "canvas",
        "module",
        "editor",
        "status",
        "form name",
        "required",
        "optional",
        "drag short answer",
        "dropdown option",
        "field",
    )
    return sum(1 for signal in moderate_signals if signal in lowered) >= 2


def _step_selector_text(step: dict[str, object], *fields: str) -> str:
    parts: list[str] = []
    for field in fields:
        value = step.get(field)
        if value is None:
            continue
        parts.append(str(value).strip().lower())
    return " ".join(part for part in parts if part).strip()


def _has_type_step_for(step: dict[str, object], needles: tuple[str, ...]) -> bool:
    if str(step.get("type") or "").strip().lower() != "type":
        return False
    text = _step_selector_text(step, "selector", "text")
    return any(needle in text for needle in needles)


def _has_click_like_step_for(step: dict[str, object], needles: tuple[str, ...]) -> bool:
    step_type = str(step.get("type") or "").strip().lower()
    if step_type not in {"click", "handle_popup"}:
        return False
    text = _step_selector_text(step, "selector", "policy")
    return any(needle in text for needle in needles)


def _looks_generic_selector(selector: object) -> bool:
    value = str(selector or "").strip().lower()
    return value in {
        "",
        "body",
        "html",
        "main",
        "form",
        "div",
        "span",
        "p",
        "button",
        "input",
        "select",
        "textarea",
        "label",
        "h1",
        "h2",
        "h3",
        "text=",
    }


def _raw_step_has_empty_verification(step: object) -> bool:
    if not isinstance(step, dict):
        return False
    if str(step.get("type") or "").strip().lower() != "verify_text":
        return False
    return not bool(extract_verify_text_value(step))


def _validate_plan_result(
    *,
    task: str,
    payload: dict[str, object],
    raw_steps: list[object],
    normalized_steps: list[dict[str, object]],
    dropped_steps: list[dict[str, object]],
) -> dict[str, object]:
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, object]] = []
    missing_steps: list[str] = []
    rejection_reasons: list[str] = []

    run_name = payload.get("run_name")
    if not isinstance(run_name, str) or not run_name.strip():
        errors.append("Plan is missing a non-empty run_name.")

    start_url = payload.get("start_url")
    if start_url is not None and not isinstance(start_url, str):
        errors.append("Plan start_url must be a string or null.")

    if not raw_steps:
        errors.append("LLM returned no raw steps.")

    if dropped_steps:
        for dropped in dropped_steps:
            raw_index = int(dropped.get("raw_index", -1))
            reason = str(dropped.get("reason") or "dropped during normalization")
            message = f"Raw step #{raw_index + 1} was dropped: {reason}"
            errors.append(message)
            rejection_reasons.append(message)

    if not normalized_steps:
        errors.append("No runnable steps remained after normalization.")

    step_counts = _summarize_step_types(normalized_steps)
    task_url = _TASK_URL_RE.search(task)
    explicit_instruction_count = _instruction_line_count(task)

    normalized_lower_task = task.lower()
    is_enterprise_prompt = _is_enterprise_prompt(task)

    unsupported_output_request = _task_contains_any(
        task,
        (
            "extract:",
            "extract ",
            "return the details",
            "return details",
            "return:",
            "output:",
            "capture the",
            "add-to-cart status",
            "checkout navigation status",
        ),
    )
    if unsupported_output_request:
        message = (
            "Prompt requests extraction/output data, but the current runtime only supports executable UI steps "
            "like navigate, click, type, select, drag, scroll, wait, verify_text, and verify_image."
        )
        errors.append(message)
        rejection_reasons.append(message)

    empty_verification_indexes = [
        index + 1 for index, raw_step in enumerate(raw_steps) if _raw_step_has_empty_verification(raw_step)
    ]
    if empty_verification_indexes:
        indexes = ", ".join(f"#{index}" for index in empty_verification_indexes[:6])
        message = (
            f"LLM produced verify_text steps without an expected value at raw step(s) {indexes}. "
            "Verification steps must include a concrete expected value."
        )
        errors.append(message)
        rejection_reasons.append(message)
        missing_steps.append("verification value")

    def _record_check(
        name: str,
        required: bool,
        passed: bool,
        detail: str,
        *,
        missing_step: str | None = None,
    ) -> None:
        checks.append(
            {
                "name": name,
                "required": required,
                "passed": passed,
                "detail": detail,
            }
        )
        if required and not passed:
            errors.append(detail)
            rejection_reasons.append(detail)
            if missing_step:
                missing_steps.append(missing_step)

    needs_navigation = bool(task_url)
    has_navigation = step_counts.get("navigate", 0) > 0 or bool(str(start_url or "").strip())
    _record_check(
        "navigate_for_url",
        needs_navigation,
        has_navigation,
        "Prompt includes a URL but the plan does not include a navigate step or start_url.",
    )

    needs_drag = _task_contains_any(task, ("drag", "drop"))
    _record_check(
        "drag_instruction",
        needs_drag,
        step_counts.get("drag", 0) > 0,
        "Prompt mentions drag/drop but the plan does not include a drag step.",
        missing_step="drag",
    )

    needs_verification = _task_contains_any(task, ("verify", "assert", "check"))
    _record_check(
        "verification_instruction",
        needs_verification,
        (step_counts.get("verify_text", 0) + step_counts.get("verify_image", 0)) > 0,
        "Prompt includes a verification instruction but the plan has no verify_text or verify_image step.",
        missing_step="verify step",
    )

    needs_wait = "wait" in task.lower()
    _record_check(
        "wait_instruction",
        needs_wait,
        step_counts.get("wait", 0) > 0,
        "Prompt includes a wait instruction but the plan has no wait step.",
        missing_step="wait step",
    )

    needs_click = _task_contains_any(task, ("click", "tap", "press"))
    _record_check(
        "click_instruction",
        needs_click,
        step_counts.get("click", 0) > 0,
        "Prompt includes a click instruction but the plan has no click step.",
        missing_step="click step",
    )

    needs_text_entry = _task_contains_any(task, ("type ", "enter ", "fill ", "input ", "email", "password", "username"))
    _record_check(
        "text_entry_instruction",
        needs_text_entry,
        step_counts.get("type", 0) > 0,
        "Prompt includes text entry instructions but the plan has no type step.",
        missing_step="type step",
    )

    needs_login = _task_contains_any(task, ("login", "sign in", "log in")) or (
        _task_contains_any(task, ("email", "username")) and "password" in normalized_lower_task
    )
    if needs_login:
        has_username_step = any(
            _has_type_step_for(step, ("username", "email", "{{selector.email}}", "{{selector.username}}", "#username"))
            for step in normalized_steps
        )
        has_password_step = any(
            _has_type_step_for(step, ("password", "{{selector.password}}", "#password"))
            for step in normalized_steps
        )
        has_login_submit = any(
            _has_click_like_step_for(
                step,
                ("login", "log in", "sign in", "submit", "{{selector.login", "{{selector.submit", "#login", "#submit"),
            )
            for step in normalized_steps
        )
        _record_check(
            "login_username_coverage",
            True,
            has_username_step,
            "Prompt looks like a login flow but the plan is missing a username/email entry step.",
            missing_step="username/email entry",
        )
        _record_check(
            "login_password_coverage",
            True,
            has_password_step,
            "Prompt looks like a login flow but the plan is missing a password entry step.",
            missing_step="password entry",
        )
        _record_check(
            "login_submit_coverage",
            True,
            has_login_submit,
            "Prompt looks like a login flow but the plan is missing a submit/login click step.",
            missing_step="login submit click",
        )

    needs_form_fill = _task_contains_any(
        task,
        (
            "fill",
            "enter",
            "type",
            "select",
            "dropdown",
            "form name",
            "field",
            "editor",
            "optional",
            "required",
        ),
    ) and not needs_login
    if needs_form_fill:
        has_type_or_select = (step_counts.get("type", 0) + step_counts.get("select", 0)) > 0
        _record_check(
            "form_fill_coverage",
            True,
            has_type_or_select,
            "Prompt includes form filling instructions but the plan has no type or select steps.",
            missing_step="form fill step",
        )

    if needs_drag:
        drag_steps = [step for step in normalized_steps if str(step.get("type") or "").strip().lower() == "drag"]
        valid_drag_steps = [
            step
            for step in drag_steps
            if str(step.get("source_selector") or "").strip() and str(step.get("target_selector") or "").strip()
        ]
        _record_check(
            "drag_step_shape",
            True,
            bool(valid_drag_steps),
            "Prompt mentions drag/drop but the plan does not include a valid drag step with both source_selector and target_selector.",
            missing_step="valid drag step",
        )

    verify_steps = [
        step for step in normalized_steps if str(step.get("type") or "").strip().lower() in {"verify_text", "verify_image"}
    ]
    if needs_verification:
        has_specific_verify = False
        for step in verify_steps:
            if str(step.get("type") or "").strip().lower() == "verify_text":
                selector = step.get("selector")
                value = str(step.get("value") or "").strip()
                if value and not _looks_generic_selector(selector):
                    has_specific_verify = True
                    break
                if value and str(selector or "").strip().lower().startswith("text="):
                    has_specific_verify = True
                    break
            else:
                selector = step.get("selector")
                if selector and not _looks_generic_selector(selector):
                    has_specific_verify = True
                    break
        _record_check(
            "verification_specificity",
            True if is_enterprise_prompt else False,
            has_specific_verify or not is_enterprise_prompt,
            "Enterprise/workflow prompt includes verification but the plan verification is vague or uses only generic selectors.",
            missing_step="specific verification target",
        )

    vague_selector_steps = [
        step
        for step in normalized_steps
        if str(step.get("type") or "").strip().lower() in {"click", "type", "select", "verify_text"}
        and _looks_generic_selector(step.get("selector"))
    ]
    if is_enterprise_prompt and vague_selector_steps:
        warning_detail = "Enterprise/workflow plan contains generic selectors for actionable steps."
        warnings.append(warning_detail)
        if len(vague_selector_steps) >= 2:
            errors.append(f"{warning_detail} Use stable, specific selectors instead of generic tags.")
            rejection_reasons.append(f"{warning_detail} Use stable, specific selectors instead of generic tags.")

    if explicit_instruction_count >= 3 and len(normalized_steps) < max(2, explicit_instruction_count - 1):
        warnings.append(
            "Plan has fewer runnable steps than expected from the number of prompt instructions; review for missing actions."
        )
        if is_enterprise_prompt:
            errors.append("Enterprise/workflow prompt appears incomplete because the generated plan covers too few of the requested actions.")
            rejection_reasons.append(
                "Enterprise/workflow prompt appears incomplete because the generated plan covers too few of the requested actions."
            )
        elif len(normalized_steps) < max(2, explicit_instruction_count // 2):
            errors.append("Generated plan appears incomplete because it covers too few of the requested actions.")
            rejection_reasons.append("Generated plan appears incomplete because it covers too few of the requested actions.")

    if is_enterprise_prompt and len(normalized_steps) <= 1:
        errors.append("Enterprise/workflow prompt produced an incomplete plan with too few steps.")
        rejection_reasons.append("Enterprise/workflow prompt produced an incomplete plan with too few steps.")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
        "missing_steps": sorted(set(missing_steps)),
        "rejection_reasons": rejection_reasons,
        "is_enterprise_prompt": is_enterprise_prompt,
        "raw_step_count": len(raw_steps),
        "normalized_step_count": len(normalized_steps),
        "step_type_counts": step_counts,
    }


def _validation_retry_prompt(base_task: str, errors: list[str]) -> str:
    bullet_lines = "\n".join(f"- {message}" for message in errors)
    return (
        f"{base_task}\n\n"
        "Validation feedback from the previous rejected plan:\n"
        f"{bullet_lines}\n"
        "Enterprise planning requirements:\n"
        "- If the prompt includes login, include username/email entry, password entry, and a submit/login click step.\n"
        "- If the prompt includes drag/drop, include valid drag steps with both source_selector and target_selector.\n"
        "- If the prompt includes form filling, include type/select steps for the mentioned fields.\n"
        "- If the prompt includes verification, include verify steps with specific, non-generic targets.\n"
        "- Reject vague selectors such as body, form, div, h1, generic button, or generic input when the control is specific.\n"
        "Return a fully corrected strict JSON plan that covers every required instruction. "
        "Do not omit steps that previously failed validation."
    )


def _should_use_structured_parse(task: str, parsed_steps: list[dict[str, object]]) -> bool:
    if not parsed_steps:
        return False
    instruction_count = _instruction_line_count(task)
    if instruction_count <= 0:
        return False
    coverage = len(parsed_steps) / instruction_count
    lower_task = task.lower()
    has_template_selector = any(
        isinstance(step.get("selector"), str) and "{{selector." in step.get("selector", "")
        for step in parsed_steps
    )
    has_explicit_selector_syntax = any(token in task for token in ("#", "[", "]", "text=", "xpath=", ":has-text("))
    specialized_keywords = (
        "vita",
        "workflow",
        "create form",
        "form canvas",
        "add status",
        "transition",
    )
    is_specialized_prompt = any(keyword in lower_task for keyword in specialized_keywords)
    if not (has_template_selector or has_explicit_selector_syntax or is_specialized_prompt):
        return False
    # Structured parser is Vita/workflow-specific. For non-Vita prompts with template
    # selectors, prefer the LLM planner to avoid low-quality partial plans.
    if "vita" not in lower_task and has_template_selector:
        return False
    return coverage >= 0.6


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

        lower_source = source_selector.lower()
        skip_pre_click_for_drag_source = (
            lower_source in {
                "{{selector.short_answer_source}}",
                "{{selector.email_field_source}}",
                "{{selector.dropdown_field_source}}",
            }
            or "[draggable='true']" in lower_source
            or "data-rbd-draggable-id" in lower_source
            or "field-short-answer" in lower_source
            or "field-email" in lower_source
            or "field-dropdown" in lower_source
        )

        prev = expanded[-1] if expanded else None
        prev_is_same_click = bool(
            prev
            and str(prev.get("type", "")).lower() == "click"
            and str(prev.get("selector") or "").strip() == source_selector
        )

        if (
            auto_drag_pre_click_enabled
            and not skip_pre_click_for_drag_source
            and not prev_is_same_click
            and len(expanded) < max_steps
        ):
            expanded.append({"type": "click", "selector": source_selector})

        if len(expanded) < max_steps:
            expanded.append(dict(step))

        if auto_drag_post_wait_ms > 0 and len(expanded) < max_steps:
            expanded.append({"type": "wait", "until": "timeout", "ms": auto_drag_post_wait_ms})

    return expanded[:max_steps]


def build_app() -> FastAPI:
    settings = get_settings()
    log_level_name = str(settings.log_level).upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    logging.basicConfig(level=log_level)

    app = FastAPI(title="Tekno Phantom Agent API", version="0.1.0")
    init_auth_database()
    if settings.auth_bootstrap_admin_email and settings.auth_bootstrap_admin_password:
        with db_session() as db:
            ensure_bootstrap_admin(
                db,
                email=settings.auth_bootstrap_admin_email,
                password=settings.auth_bootstrap_admin_password,
            )
    app.include_router(auth_router)

    @app.middleware("http")
    async def ensure_csrf_cookie(request: Request, call_next):
        response = await call_next(request)
        if not request.cookies.get(settings.auth_csrf_cookie_name):
            set_csrf_cookie(response, csrf_token=issue_csrf_token(), settings=settings)
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
    require_api_auth = build_api_auth_dependency(settings)

    def _owned_folder_or_404(folder_id: str, user: User) -> FolderState:
        folder = test_case_store.get_folder(folder_id)
        if not folder or folder.user_id != user.id:
            raise HTTPException(status_code=404, detail="Folder not found")
        return folder

    def _owned_test_case_or_404(test_case_id: str, user: User) -> TestCaseState:
        test_case = test_case_store.get(test_case_id)
        if not test_case or test_case.user_id != user.id:
            raise HTTPException(status_code=404, detail="Test case not found")
        return test_case

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
        _: object | None = Depends(require_api_auth),
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
                "prompt": request.prompt,
                "execution_mode": request.execution_mode,
                "failure_mode": request.failure_mode,
                "steps": expanded_steps,
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

        run = run_store.create(expanded_request)
        background_tasks.add_task(executor.execute, run.run_id)
        LOGGER.info("Run created: %s", run.run_id)
        return run

    @app.post("/api/test-cases", response_model=TestCaseState)
    async def create_test_case(
        request: TestCaseCreateRequest,
        user: User = Depends(get_current_user),
    ) -> TestCaseState:
        if request.parent_folder_id:
            _owned_folder_or_404(request.parent_folder_id, user)
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
        test_case = test_case_store.create(validated_request, user.id)
        return test_case

    @app.post("/api/test-folders", response_model=FolderState)
    async def create_test_folder(
        request: FolderCreateRequest,
        user: User = Depends(get_current_user),
    ) -> FolderState:
        if request.parent_folder_id:
            _owned_folder_or_404(request.parent_folder_id, user)
        folder = test_case_store.create_folder(
            FolderCreateRequest.model_validate(
                {
                    "name": request.name,
                    "parent_folder_id": request.parent_folder_id,
                }
            ),
            user.id,
        )
        return folder

    @app.get("/api/test-folders", response_model=FolderListResponse)
    async def list_test_folders(user: User = Depends(get_current_user)) -> FolderListResponse:
        return FolderListResponse(items=[folder for folder in test_case_store.list_folders() if folder.user_id == user.id])

    @app.delete("/api/test-folders/{folder_id}", status_code=204)
    async def delete_test_folder(
        folder_id: str,
        user: User = Depends(get_current_user),
    ) -> None:
        folders = [folder for folder in test_case_store.list_folders() if folder.user_id == user.id]
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

        test_cases = [test_case for test_case in test_case_store.list() if test_case.user_id == user.id]
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
        _: object | None = Depends(require_api_auth),
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
    async def list_test_cases(user: User = Depends(get_current_user)) -> TestCaseListResponse:
        return TestCaseListResponse(items=[item for item in test_case_store.list() if item.user_id == user.id])

    @app.get("/api/test-cases/{test_case_id}", response_model=TestCaseState)
    async def get_test_case(
        test_case_id: str,
        user: User = Depends(get_current_user),
    ) -> TestCaseState:
        return _owned_test_case_or_404(test_case_id, user)

    @app.put("/api/test-cases/{test_case_id}", response_model=TestCaseState)
    async def update_test_case(
        test_case_id: str,
        request: TestCaseUpdateRequest,
        user: User = Depends(get_current_user),
    ) -> TestCaseState:
        current = _owned_test_case_or_404(test_case_id, user)
        if request.parent_folder_id:
            _owned_folder_or_404(request.parent_folder_id, user)
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
        user: User = Depends(get_current_user),
    ) -> None:
        _owned_test_case_or_404(test_case_id, user)
        deleted = test_case_store.delete(test_case_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Test case not found")

    @app.post("/api/test-cases/{test_case_id}/run", response_model=RunState)
    async def run_test_case(
        test_case_id: str,
        background_tasks: BackgroundTasks,
        user: User = Depends(get_current_user),
    ) -> RunState:
        test_case = _owned_test_case_or_404(test_case_id, user)

        run_request = RunCreateRequest.model_validate(
            {
                "run_name": test_case.name,
                "start_url": test_case.start_url,
                "steps": [step.model_dump(exclude_none=True) for step in test_case.steps],
                "test_data": test_case.test_data,
                "selector_profile": test_case.selector_profile,
                "source_test_case_id": test_case.test_case_id,
                "failure_mode": settings.step_failure_mode,
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
                "source_test_case_id": test_case.test_case_id,
                "failure_mode": settings.step_failure_mode,
            }
        )
        run = run_store.create(expanded_run_request)
        background_tasks.add_task(executor.execute, run.run_id)
        LOGGER.info("Run created from test case: %s (test_case_id=%s)", run.run_id, test_case_id)
        return run

    @app.post("/api/plan", response_model=PlanGenerateResponse)
    async def generate_plan(
        request: PlanGenerateRequest,
        _: object | None = Depends(require_api_auth),
    ) -> PlanGenerateResponse:
        trace_run_id = f"plan-{uuid4()}"
        max_steps = request.max_steps or settings.max_steps_per_run
        max_steps = min(max_steps, settings.max_steps_per_run)
        structured_parse_validation: dict[str, object] | None = None
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
        should_use_structured_parse = _should_use_structured_parse(request.task, parsed_steps)
        if should_use_structured_parse:
            structured_parse_validation = _validate_plan_result(
                task=request.task,
                payload={"run_name": "prompt-steps-run", "start_url": None},
                raw_steps=parsed_steps,
                normalized_steps=parsed_steps,
                dropped_steps=[],
            )
        if should_use_structured_parse and structured_parse_validation and structured_parse_validation.get("valid"):
            validated = RunCreateRequest.model_validate(
                {
                    "run_name": "prompt-steps-run",
                    "start_url": None,
                    "steps": parsed_steps,
                    "test_data": request.test_data,
                    "selector_profile": request.selector_profile,
                }
            )
            await file_client.write_text_artifact(
                trace_run_id,
                "plan-trace.json",
                _trace_json(
                    {
                        "run_id": trace_run_id,
                        "stage": "plan_generation",
                        "source": "structured_parser",
                        "task": request.task,
                        "max_steps": max_steps,
                        "raw_llm_response": None,
                        "raw_steps": parsed_steps,
                        "validation": structured_parse_validation,
                        "normalized_plan": {
                            "run_name": validated.run_name,
                            "start_url": validated.start_url,
                            "steps": [step.model_dump(exclude_none=True) for step in validated.steps[:max_steps]],
                        },
                        "dropped_steps": [],
                        "modified_steps": [],
                    }
                ),
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

        final_payload: dict[str, object] | None = None
        final_raw_steps: list[object] = []
        final_dropped_steps: list[dict[str, object]] = []
        final_modified_steps: list[dict[str, object]] = []
        final_validation: dict[str, object] | None = None
        validated: RunCreateRequest | None = None
        planning_attempts: list[dict[str, object]] = []
        current_planning_task = planning_task

        for attempt_index in range(2):
            try:
                payload = await brain_client.plan_task(current_planning_task, max_steps=max_steps)
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"Brain plan generation failed: {exc}") from exc

            raw_steps = payload.get("steps")
            if not isinstance(raw_steps, list):
                raw_steps = []

            try:
                normalized_before_sanitize = normalize_plan_steps(
                    raw_steps,
                    max_steps=max_steps,
                    default_wait_ms=settings.planner_default_wait_ms,
                )
                dropped_steps, modified_steps = _plan_step_deltas(raw_steps, normalized_before_sanitize)
                normalized_steps = _sanitize_plan_steps(normalized_before_sanitize, start_url=payload.get("start_url"))
                if normalized_steps != normalized_before_sanitize:
                    modified_steps.append(
                        {
                            "stage": "sanitize_plan_steps",
                            "before": normalized_before_sanitize,
                            "after": normalized_steps,
                        }
                    )
                ensured_steps = _ensure_drag_step(request.task, normalized_steps)
                if ensured_steps != normalized_steps:
                    modified_steps.append(
                        {
                            "stage": "ensure_drag_step",
                            "before": normalized_steps,
                            "after": ensured_steps,
                        }
                    )
                normalized_steps = _expand_drag_steps(
                    ensured_steps,
                    max_steps=max_steps,
                    auto_drag_pre_click_enabled=settings.auto_drag_pre_click_enabled,
                    auto_drag_post_wait_ms=settings.auto_drag_post_wait_ms,
                )
                if normalized_steps != ensured_steps:
                    modified_steps.append(
                        {
                            "stage": "expand_drag_steps",
                            "before": ensured_steps,
                            "after": normalized_steps,
                        }
                    )
                validation = _validate_plan_result(
                    task=request.task,
                    payload=payload,
                    raw_steps=raw_steps,
                    normalized_steps=normalized_steps,
                    dropped_steps=dropped_steps,
                )
                planning_attempts.append(
                    {
                        "attempt": attempt_index + 1,
                        "planning_task": current_planning_task,
                        "raw_llm_response": payload.get("raw_llm_response"),
                        "raw_steps": raw_steps,
                        "normalized_steps": normalized_steps,
                        "dropped_steps": dropped_steps,
                        "modified_steps": modified_steps,
                        "validation": validation,
                    }
                )

                final_payload = payload
                final_raw_steps = raw_steps
                final_dropped_steps = dropped_steps
                final_modified_steps = modified_steps
                final_validation = validation

                if validation.get("valid"):
                    validated = RunCreateRequest.model_validate(
                        {
                            "run_name": payload.get("run_name", "ai-generated-run"),
                            "start_url": payload.get("start_url"),
                            "steps": normalized_steps,
                            "test_data": request.test_data,
                            "selector_profile": request.selector_profile,
                        }
                    )
                    break

                LOGGER.warning(
                    "Plan validation rejected planning attempt %s for %s: %s",
                    attempt_index + 1,
                    trace_run_id,
                    "; ".join(str(item) for item in validation.get("errors", [])),
                )
                if attempt_index == 0:
                    current_planning_task = _validation_retry_prompt(
                        planning_task,
                        [str(item) for item in validation.get("errors", [])],
                    )
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"Invalid plan returned by brain: {exc}") from exc

        if validated is None or final_payload is None or final_validation is None:
            await file_client.write_text_artifact(
                trace_run_id,
                "plan-trace.json",
                _trace_json(
                    {
                        "run_id": trace_run_id,
                        "stage": "plan_generation",
                        "source": "brain_llm",
                        "task": request.task,
                        "planning_task": planning_task,
                        "max_steps": max_steps,
                        "structured_parser_rejected": structured_parse_validation,
                        "raw_llm_response": (final_payload or {}).get("raw_llm_response") if final_payload else None,
                        "raw_steps": final_raw_steps,
                        "normalized_plan": None,
                        "dropped_steps": final_dropped_steps,
                        "modified_steps": final_modified_steps,
                        "validation": final_validation,
                        "attempts": planning_attempts,
                    }
                ),
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Generated plan failed validation after one retry.",
                    "validation_errors": list(final_validation.get("errors", [])),
                },
            )

        await file_client.write_text_artifact(
            trace_run_id,
            "plan-trace.json",
            _trace_json(
                {
                    "run_id": trace_run_id,
                    "stage": "plan_generation",
                    "source": "brain_llm",
                    "task": request.task,
                    "planning_task": planning_task,
                    "max_steps": max_steps,
                    "structured_parser_rejected": structured_parse_validation,
                    "raw_llm_response": final_payload.get("raw_llm_response"),
                    "raw_steps": final_raw_steps,
                    "normalized_plan": {
                        "run_name": validated.run_name,
                        "start_url": validated.start_url,
                        "steps": [step.model_dump(exclude_none=True) for step in validated.steps[:max_steps]],
                    },
                    "dropped_steps": final_dropped_steps,
                    "modified_steps": final_modified_steps,
                    "validation": final_validation,
                    "attempts": planning_attempts,
                }
            ),
        )

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
        user: User = Depends(get_current_user),
    ) -> SuiteRunState:
        all_test_cases = [item for item in test_case_store.list() if item.user_id == user.id]
        test_by_id = {item.test_case_id: item for item in all_test_cases}

        target_ids: list[str] = list(request.test_case_ids)
        if request.folder_id:
            all_folders = [item for item in test_case_store.list_folders() if item.user_id == user.id]
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
            detail = test_by_id.get(case_id)
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
        _: object | None = Depends(require_api_auth),
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

    @app.post("/api/runs/{run_id}/steps/{step_id}/selector", response_model=RunState)
    async def submit_step_selector(
        run_id: str,
        step_id: str,
        request: StepSelectorHelpRequest,
        background_tasks: BackgroundTasks,
        _: object | None = Depends(require_api_auth),
    ) -> RunState:
        try:
            updated = executor.apply_manual_selector_hint(run_id, step_id, request.selector)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not updated:
            raise HTTPException(status_code=404, detail="Run or step not found")

        background_tasks.add_task(executor.execute, updated.run_id)
        LOGGER.info(
            "Manual selector submitted: run_id=%s step_id=%s selector=%s",
            updated.run_id,
            step_id,
            request.selector,
        )
        return updated

    @app.post("/api/runs/{run_id}/resume", response_model=RunState)
    async def resume_run(
        run_id: str,
        request: RunResumeRequest,
        background_tasks: BackgroundTasks,
        _: object | None = Depends(require_api_auth),
    ) -> RunState:
        run = run_store.get(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        failed_step = next(
            (step for step in run.steps if step.status in {"failed", "waiting_for_input"}),
            None,
        )
        if not failed_step:
            raise HTTPException(status_code=400, detail="Run has no failed step to resume from")

        raw_steps = [dict(step.input or {}) for step in run.steps]
        resume_steps = raw_steps[failed_step.index:]
        sanitized_steps = _sanitize_plan_steps(resume_steps, start_url=run.start_url)
        expanded_steps = _expand_drag_steps(
            sanitized_steps,
            max_steps=settings.max_steps_per_run,
            auto_drag_pre_click_enabled=settings.auto_drag_pre_click_enabled,
            auto_drag_post_wait_ms=settings.auto_drag_post_wait_ms,
        )
        run_request = RunCreateRequest.model_validate(
            {
                "run_name": request.run_name or f"{run.run_name} [resume-step-{failed_step.index + 1}]",
                "start_url": run.start_url,
                "prompt": run.prompt,
                "execution_mode": run.execution_mode,
                "failure_mode": run.failure_mode,
                "steps": expanded_steps,
                "test_data": run.test_data,
                "selector_profile": run.selector_profile,
                "source_test_case_id": run.source_test_case_id,
                "resume_from_step_index": failed_step.index,
            }
        )

        resumed = run_store.create(run_request)
        background_tasks.add_task(executor.execute, resumed.run_id)
        LOGGER.info(
            "Run resumed from failed step: new_run_id=%s source_run_id=%s step_index=%s",
            resumed.run_id,
            run_id,
            failed_step.index,
        )
        return resumed

    @app.post("/api/runs/{run_id}/recover-selector", response_model=RunState)
    async def recover_run_selector(
        run_id: str,
        request: SelectorRecoveryRequest,
        background_tasks: BackgroundTasks,
        _: object | None = Depends(require_api_auth),
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
                "prompt": run.prompt,
                "execution_mode": run.execution_mode,
                "failure_mode": run.failure_mode,
                "steps": expanded_steps,
                "test_data": run.test_data,
                "selector_profile": run.selector_profile,
                "source_test_case_id": run.source_test_case_id,
                "resume_from_step_index": request.step_index,
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
    async def cancel_run(run_id: str, _: object | None = Depends(require_api_auth)) -> CancelRunResponse:
        run = run_store.mark_cancelled(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        return CancelRunResponse(run_id=run_id, status=run.status)

    return app


app = build_app()
