from __future__ import annotations
from pathlib import Path
import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from app.brain.base import BrainClient
from app.config import Settings
from app.mcp.browser_client import BrowserMCPClient
from app.mcp.filesystem_client import FileSystemClient
from app.runtime.selector_memory import SelectorMemoryStore
from app.runtime.store import RunStore
from app.schemas import RunState, RunStatus, StepRuntimeState, StepStatus

LOGGER = logging.getLogger("tekno.phantom.executor")
TEMPLATE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")
DEFAULT_SELECTOR_PROFILE: dict[str, list[str]] = {
    "email": [
        "#username",
        "input[name='username']",
        "input[type='email']",
        "input[autocomplete='email']",
        "input[type='text']",
    ],
    "username": [
        "#username",
        "input[name='username']",
        "input[type='text']",
    ],
    "password": [
        "#password",
        "input[name='password']",
        "input[type='password']",
    ],
    "login_button": [
        "button[name='login']",
        "button[type='submit']",
        "text=Sign In",
        "text=Login",
    ],
    "create_form": [
        "button#createForm",
        "button#create_form",
        "button:has-text('Create Form')",
        "[role='button']:has-text('Create Form')",
    ],
    "create_form_confirm": [
        "[role='dialog'] button:has-text('Create')",
        "div[role='dialog'] button:has-text('Create')",
        "button:has-text('Create')",
    ],
    "form_name": [
        "input[name='formName']",
        "input[name='name']",
        "input#formName",
        "input#form-name",
        "input[placeholder*='Form Name']",
        "input[placeholder*='Name']",
        "textarea[name='formName']",
        "textarea[name='name']",
    ],
    "form_list_first_row": [
        "table tbody tr:first-child",
        "[role='table'] [role='row']:nth-child(2)",
        "div[role='rowgroup'] div[role='row']:first-child",
        "[data-testid*='forms'] tr:first-child",
        "main tr:first-child",
    ],
    "form_list_first_name": [
        "table tbody tr:first-child a",
        "table tbody tr:first-child td a",
        "[role='table'] [role='row']:nth-child(2) a",
        "[data-testid*='forms'] tr:first-child a",
        "main tr:first-child a",
    ],
    "save_form": [
        "div[role='dialog'] button:has-text('Save')",
        "div[role='dialog'] [role='button']:has-text('Save')",
        "div[role='dialog'] button[type='submit']",
        "button#saveForm",
        "button.save-form",
        "button:has-text('Save')",
        "[role='button']:has-text('Save')",
    ],
    "back_button": [
        "button:has([data-lucide='chevron-left'])",
        "button:has([data-lucide='arrow-left'])",
        "button:has(svg[class*='chevron-left'])",
        "button:has(svg[class*='arrow-left'])",
        "button[aria-label*='Back']",
        "[role='button'][aria-label*='Back']",
        "button:has-text('Back')",
        "text=Back",
    ],
    "short_answer_source": [
        "[data-testid='field-short-answer']",
        "[data-testid*='short-answer']",
        "[data-rbd-draggable-id*='short']",
        "[draggable='true'][aria-label*='Short answer']",
        "[draggable='true']:has-text('Short answer')",
        "[role='listitem']:has-text('Short answer')",
        "button:has-text('Short answer')",
        "[role='button']:has-text('Short answer')",
        "text=Short answer",
    ],
    "email_field_source": [
        "[draggable='true'][aria-label*='Email']",
        "[draggable='true']:has-text('Email')",
        "[role='listitem']:has-text('Email')",
        "[data-rbd-draggable-id*='email']",
        "[data-testid='field-email']",
        "[data-testid*='field-email']",
        "button:has-text('Email')",
        "[role='button']:has-text('Email')",
        "text=Email",
    ],
    "dropdown_field_source": [
        "[data-testid='field-dropdown']",
        "[data-testid*='field-dropdown']",
        "[data-rbd-draggable-id*='dropdown']",
        "[draggable='true']:has-text('Dropdown')",
        "[role='listitem']:has-text('Dropdown')",
        "button:has-text('Dropdown')",
        "[role='button']:has-text('Dropdown')",
        "text=Dropdown",
    ],
    "form_canvas_target": [
        "[data-row-id].form-row[draggable='true']",
        "[data-row-id]",
        "[data-testid='form-builder-canvas']",
        ".form-canvas",
        ".form-drop-area",
        "[data-testid*='form-builder'][class*='canvas']",
        ".form-builder-canvas",
        ".form-builder-drop-area",
        ".form-builder-editor",
        "[data-testid='form-canvas']",
        "[class*='drop'][class*='canvas']",
        "[class*='builder'][class*='canvas']",
        "div.form-row[draggable='true']:has-text('Drag and drop fields here')",
        "div.form-row.relative.flex.w-full[draggable='true']:has-text('Drag and drop fields here')",
        "div:has-text('Drag and drop fields here')",
        "section:has-text('Drag and drop fields here')",
        "[role='application']",
    ],
    "form_label": [
        "div[role='dialog'] input[placeholder='Enter a label']",
        "div[role='dialog'] input[name='label']",
        "div[role='dialog'] input[aria-label*='Label']",
        "div[role='dialog'] textarea[placeholder='Enter a label']",
        "div[role='dialog'] textarea[name='label']",
        "[data-testid='form-builder-canvas'] input[placeholder='Label']",
        "[data-testid='form-builder-canvas'] textarea[placeholder='Label']",
        "[data-testid='form-builder-canvas'] input[name='label']",
        "[data-testid='form-builder-canvas'] textarea[name='label']",
        "[data-testid='form-builder-canvas'] input[aria-label*='Label']",
        "[data-testid='form-builder-canvas'] textarea[aria-label*='Label']",
        ".form-canvas input[placeholder='Label']",
        ".form-canvas textarea[placeholder='Label']",
        "input[placeholder='Label']",
        "textarea[placeholder='Label']",
        "input[name='label']",
        "textarea[name='label']",
        "input[aria-label*='Label']",
        "textarea[aria-label*='Label']",
        "[contenteditable='true'][aria-label*='Label']",
        "[role='textbox'][aria-label*='Label']",
    ],
    "required_checkbox": [
        "div[role='dialog'] label:has-text('Required')",
        "div[role='dialog'] label:has-text('Required') input[type='checkbox']",
        "div[role='dialog'] input[type='checkbox'][name='required']",
        "div[role='dialog'] [role='checkbox'][aria-label*='Required']",
        "input[name='required']",
        "input[type='checkbox'][name='required']",
        "[data-testid='required'] input[type='checkbox']",
        "label:has-text('Required')",
        "label:has-text('Required') input[type='checkbox']",
        "text=Required",
    ],
    "dropdown_option_type_trigger": [
        "div[role='dialog'] [role='combobox']:has-text('Select an option')",
        "div[role='dialog'] [role='combobox']",
        "div[role='dialog'] [aria-haspopup='listbox']",
        "div[role='dialog'] button:has-text('Select an option')",
        "text=Select an option",
    ],
    "dropdown_option_enter_manual": [
        "div[role='listbox'] [role='option']:text-is('Enter options manually')",
        "div[role='dialog'] [role='option']:text-is('Enter options manually')",
        "[role='option']:text-is('Enter options manually')",
        "div[role='listbox'] :text-is('Enter options manually')",
        "[role='option']:has-text('Enter options manually')",
        "div[role='dialog'] :text-is('Enter options manually')",
        "text=Enter options manually",
    ],
    "dropdown_options_section": [
        "div[role='dialog'] :has-text('Options')",
        "div[role='dialog'] input[placeholder='Label']",
        "div[role='dialog'] input[placeholder='Value']",
    ],
    "dropdown_option_label": [
        "div[role='dialog'] input[placeholder='Label']",
        "div[role='dialog'] input[name='label']",
    ],
    "dropdown_option_value": [
        "div[role='dialog'] input[placeholder='Value']",
        "div[role='dialog'] input[name='value']",
    ],
    "dropdown_option_add_button": [
        "div[role='dialog'] button:has(svg[class*='plus'])",
        "div[role='dialog'] button:has(i[class*='plus'])",
        "div[role='dialog'] [data-testid*='add-option']",
        "div[role='dialog'] [aria-label*='Add option']",
        "div[role='dialog'] [title*='Add option']",
        "div[role='dialog'] div:has(input[placeholder='Value']) button",
        "div[role='dialog'] button:has-text('+')",
        "div[role='dialog'] [role='button']:has-text('+')",
        "text=+",
    ],
    "amazon_search_box": [
        "#twotabsearchtextbox",
        "input[name='field-keywords']",
    ],
    "amazon_search_submit": [
        "#nav-search-submit-button",
        "input#nav-search-submit-button",
    ],
    "amazon_first_result": [
        "div[data-component-type='s-search-result'] h2 a",
        "h2 a.a-link-normal",
        "h2 a",
    ],
    "amazon_add_to_cart": [
        "#add-to-cart-button",
        "input[name='submit.add-to-cart']",
        "button[name='submit.add-to-cart']",
        "[id*='add-to-cart']",
    ],
    "amazon_cart": [
        "#nav-cart",
        "a[href*='/gp/cart/view.html']",
        "a[href*='cart']",
    ],
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AgentExecutor:
    def __init__(
        self,
        settings: Settings,
        brain_client: BrainClient,
        run_store: RunStore,
        browser_client: BrowserMCPClient,
        file_client: FileSystemClient,
        selector_memory_store: SelectorMemoryStore | None = None,
    ) -> None:
        self._settings = settings
        self._brain = brain_client
        self._run_store = run_store
        self._browser = browser_client
        self._files = file_client
        self._selector_memory = selector_memory_store

    async def execute(self, run_id: str) -> None:
        run = self._run_store.get(run_id)
        if not run:
            return

        run.status = RunStatus.running
        run.started_at = utc_now()
        self._run_store.persist(run)

        try:
            await self._browser.start_run(run_id)

            if run.start_url:
                await asyncio.wait_for(
                    self._browser.navigate(run.start_url),
                    timeout=self._settings.step_timeout_seconds,
                )

            has_step_failure = False
            for step in run.steps:
                if self._run_store.is_cancelled(run_id):
                    step.status = StepStatus.cancelled
                    run.status = RunStatus.cancelled
                    self._run_store.persist(run)
                    break

                await self._execute_step(run, step)
                self._run_store.persist(run)
                if step.status == StepStatus.failed:
                    has_step_failure = True

            if run.status == RunStatus.running:
                run.status = RunStatus.failed if has_step_failure else RunStatus.completed
                self._run_store.persist(run)

            summary_text = self._build_summary(run)
            run.summary = await self._brain.summarize(summary_text)
            await self._files.write_text_artifact(run_id, "summary.txt", run.summary)
            self._run_store.persist(run)
        except Exception as exc:
            run.status = RunStatus.failed
            run.summary = f"Run failed unexpectedly ({type(exc).__name__}): {exc!r}"
            self._run_store.persist(run)
            LOGGER.exception("Run %s failed unexpectedly", run_id)
        finally:
            await self._browser.close_run(run_id)
            run.finished_at = utc_now()
            self._run_store.persist(run)
            self._run_store.clear_cancel(run_id)

    async def _execute_step(self, run: RunState, step: StepRuntimeState) -> None:
        step.status = StepStatus.running
        step.started_at = utc_now()
        step.error = None
        step.message = None

        try:
            message = await asyncio.wait_for(
                self._dispatch_step(run, step.input),
                timeout=self._settings.step_timeout_seconds,
            )
            step.status = StepStatus.completed
            step.message = message
            await self._files.write_text_artifact(
                run.run_id,
                f"step-{step.index:03d}.log",
                f"{step.type}: {message}",
            )
        except Exception as exc:
            step.status = StepStatus.failed
            compact = self._compact_error(exc)
            if isinstance(exc, TimeoutError):
                step.error = f"{compact} (step_type={step.type})"
            else:
                step.error = compact
            step.message = "Step failed"
        finally:
            step.ended_at = utc_now()

    async def _dispatch_step(self, run: RunState, raw_step: dict) -> str:
        step_type = raw_step.get("type")
        test_data = run.test_data or {}
        selector_profile = run.selector_profile or {}
        run_domain = self._extract_run_domain(run)

        if step_type == "navigate":
            target_url = self._apply_template(str(raw_step["url"]), test_data)
            return await self._browser.navigate(target_url)

        if step_type == "click":
            selector = str(raw_step["selector"])
            return await self._run_with_selector_fallback(
                selector,
                step_type,
                selector_profile,
                test_data,
                run_domain,
                lambda resolved: self._browser.click(resolved),
            )

        if step_type == "type":
            selector = str(raw_step["selector"])
            text = self._apply_template(str(raw_step["text"]), test_data)
            clear_first = bool(raw_step.get("clear_first", True))
            return await self._run_with_selector_fallback(
                selector,
                step_type,
                selector_profile,
                test_data,
                run_domain,
                lambda resolved: self._browser.type_text(
                    selector=resolved,
                    text=text,
                    clear_first=clear_first,
                ),
                text_hint=text,
            )

        if step_type == "select":
            selector = str(raw_step["selector"])
            value = self._apply_template(str(raw_step["value"]), test_data)
            return await self._run_with_selector_fallback(
                selector,
                step_type,
                selector_profile,
                test_data,
                run_domain,
                lambda resolved: self._browser.select(
                    selector=resolved,
                    value=value,
                ),
                text_hint=value,
            )

        if step_type == "drag":
            source_selector = str(raw_step["source_selector"])
            target_selector = str(raw_step["target_selector"])
            target_offset_x = raw_step.get("target_offset_x")
            target_offset_y = raw_step.get("target_offset_y")
            return await self._run_with_drag_fallback(
                raw_source_selector=source_selector,
                raw_target_selector=target_selector,
                selector_profile=selector_profile,
                test_data=test_data,
                run_domain=run_domain,
                target_offset_x=int(target_offset_x) if target_offset_x is not None else None,
                target_offset_y=int(target_offset_y) if target_offset_y is not None else None,
            )

        if step_type == "scroll":
            target = str(raw_step.get("target", "page"))
            selector = raw_step.get("selector")
            direction = str(raw_step.get("direction", "down"))
            amount = int(raw_step.get("amount", 600))

            if target == "selector" and selector:
                resolved_selector = await self._resolve_selector(
                    str(selector),
                    step_type,
                    selector_profile,
                    test_data,
                    run_domain,
                )
                return await self._browser.scroll(
                    target=target,
                    selector=resolved_selector,
                    direction=direction,
                    amount=amount,
                )

            return await self._browser.scroll(
                target=target,
                selector=None,
                direction=direction,
                amount=amount,
            )

        if step_type == "wait":
            until = str(raw_step.get("until", "timeout"))
            selector = raw_step.get("selector")
            load_state = raw_step.get("load_state")
            ms = raw_step.get("ms")

            if until in {"selector_visible", "selector_hidden"} and selector:
                return await self._run_with_selector_fallback(
                    str(selector),
                    step_type,
                    selector_profile,
                    test_data,
                    run_domain,
                    lambda resolved: self._browser.wait_for(
                        until=until,
                        ms=ms,
                        selector=resolved,
                        load_state=load_state,
                    ),
                )

            return await self._browser.wait_for(
                until=until,
                ms=ms,
                selector=str(selector) if selector else None,
                load_state=str(load_state) if load_state else None,
            )

        if step_type == "handle_popup":
            policy = str(raw_step.get("policy", "dismiss"))
            selector = raw_step.get("selector")
            if selector:
                return await self._run_with_selector_fallback(
                    str(selector),
                    step_type,
                    selector_profile,
                    test_data,
                    run_domain,
                    lambda resolved: self._browser.handle_popup(
                        policy=policy,
                        selector=resolved,
                    ),
                )
            return await self._browser.handle_popup(policy=policy, selector=None)

        if step_type == "verify_text":
            selector = str(raw_step["selector"])
            match = str(raw_step.get("match", "contains"))
            value = self._apply_template(str(raw_step["value"]), test_data)
            return await self._run_with_selector_fallback(
                selector,
                step_type,
                selector_profile,
                test_data,
                run_domain,
                lambda resolved: self._browser.verify_text(
                    selector=resolved,
                    match=match,
                    value=value,
                ),
                text_hint=value,
            )

        if step_type == "verify_image":
            baseline_path = raw_step.get("baseline_path")
            threshold = float(raw_step.get("threshold", 0.05))
            selector = raw_step.get("selector")

            resolved_baseline = (
                self._apply_template(str(baseline_path), test_data) if baseline_path is not None else None
            )
            if selector:
                return await self._run_with_selector_fallback(
                    str(selector),
                    step_type,
                    selector_profile,
                    test_data,
                    run_domain,
                    lambda resolved: self._browser.verify_image(
                        selector=resolved,
                        baseline_path=resolved_baseline,
                        threshold=threshold,
                    ),
                )
            return await self._browser.verify_image(
                selector=None,
                baseline_path=resolved_baseline,
                threshold=threshold,
            )

        raise ValueError(f"Unsupported step type: {step_type}")

    async def _resolve_selector(
        self,
        raw_selector: str,
        step_type: str,
        selector_profile: dict[str, list[str]],
        test_data: dict[str, Any],
        run_domain: str | None,
        text_hint: str | None = None,
    ) -> str:
        candidates = self._selector_candidates(
            raw_selector,
            step_type,
            selector_profile,
            test_data,
            run_domain,
            text_hint,
        )
        if not candidates:
            raise ValueError("No selector candidates available")
        return candidates[0]

    async def _run_with_selector_fallback(
        self,
        raw_selector: str,
        step_type: str,
        selector_profile: dict[str, list[str]],
        test_data: dict[str, Any],
        run_domain: str | None,
        operation: Callable[[str], Awaitable[str]],
        text_hint: str | None = None,
    ) -> str:
        candidates = self._selector_candidates(
            raw_selector,
            step_type,
            selector_profile,
            test_data,
            run_domain,
            text_hint,
        )
        last_error: Exception | None = None
        candidate_timeout_s = self._candidate_timeout_seconds(len(candidates))
        attempts: list[str] = []
        recovery_attempts = self._selector_recovery_attempts()

        for cycle in range(recovery_attempts):
            for selector in candidates:
                try:
                    result = await asyncio.wait_for(operation(selector), timeout=candidate_timeout_s)
                    self._remember_selector_success(
                        run_domain=run_domain,
                        step_type=step_type,
                        raw_selector=raw_selector,
                        resolved_selector=selector,
                        text_hint=text_hint,
                    )
                    return result
                except Exception as exc:
                    last_error = exc
                    attempts.append(f"pass {cycle + 1}: {selector} -> {self._compact_error(exc)}")

            if cycle >= recovery_attempts - 1:
                break
            if not self._should_retry_selector_error(last_error):
                break
            await self._selector_recovery_pause()

        if last_error:
            if attempts:
                attempted = "; ".join(attempts)
                raise ValueError(f"All selector candidates failed: {attempted}") from last_error
            raise last_error
        raise ValueError(f"No valid selector candidates for: {raw_selector}")

    def _candidate_timeout_seconds(self, candidate_count: int) -> float:
        step_timeout = max(float(self._settings.step_timeout_seconds), 1.0)
        if candidate_count <= 1:
            return step_timeout
        budget = max(step_timeout - 0.5, 1.0)
        per_candidate = budget / candidate_count
        return max(min(per_candidate, step_timeout), 1.0)

    def _selector_candidates(
        self,
        raw_selector: str,
        step_type: str,
        selector_profile: dict[str, list[str]],
        test_data: dict[str, Any],
        run_domain: str | None,
        text_hint: str | None = None,
    ) -> list[str]:
        selector = self._apply_template(raw_selector, test_data).strip()
        if not selector:
            return []

        keys: list[str] = []
        alias_key = self._selector_alias_key(selector)
        if alias_key:
            keys.append(alias_key)

        signal_parts = [selector.lower(), step_type.lower()]
        if text_hint:
            signal_parts.append(text_hint.lower())
        signal = " ".join(signal_parts)
        profile_keys = list(selector_profile.keys())
        for key in profile_keys:
            key_lower = key.lower()
            if key_lower and key_lower in signal:
                keys.append(key)

        if step_type == "type" and text_hint:
            hint_lower = text_hint.lower()
            if "@" in text_hint or "email" in hint_lower:
                keys.insert(0, "email")
                keys.append("username")
            if "password" in hint_lower:
                keys.insert(0, "password")
            if "qa_form" in hint_lower or "form" in hint_lower and "name" in hint_lower:
                keys.insert(0, "form_name")
            if "first name" in hint_lower or "label" in hint_lower:
                keys.insert(0, "form_label")

        selector_lower = selector.lower()
        if step_type == "type":
            if "email" in selector_lower:
                keys.insert(0, "email")
                keys.append("username")
            if "password" in selector_lower:
                keys.insert(0, "password")
            if "username" in selector_lower:
                keys.insert(0, "username")
            if "formname" in selector_lower or "form_name" in selector_lower or "form name" in selector_lower:
                keys.insert(0, "form_name")
            if "label" in selector_lower:
                keys.insert(0, "form_label")
            if "dropdown_option_label" in selector_lower:
                keys.insert(0, "dropdown_option_label")
            if "dropdown_option_value" in selector_lower:
                keys.insert(0, "dropdown_option_value")
            if any(token in selector_lower for token in ("twotabsearchtextbox", "field-keywords")):
                keys.insert(0, "amazon_search_box")
        if step_type == "click":
            if any(token in selector_lower for token in ("login", "sign in", "signin", "log in")):
                keys.insert(0, "login_button")
            if "create form" in selector_lower or "create_form" in selector_lower or "createform" in selector_lower:
                keys.insert(0, "create_form")
            if "form_list_first_name" in selector_lower:
                keys.insert(0, "form_list_first_name")
            if "back button" in selector_lower or "selector.back_button" in selector_lower or selector_lower.strip() == "back":
                keys.insert(0, "back_button")
            if "save form" in selector_lower or "save_form" in selector_lower or "saveform" in selector_lower:
                keys.insert(0, "save_form")
            if any(token in selector_lower for token in ("required", "checkbox")):
                keys.insert(0, "required_checkbox")
            if "dropdown_option_type_trigger" in selector_lower:
                keys.insert(0, "dropdown_option_type_trigger")
            if "dropdown_option_enter_manual" in selector_lower:
                keys.insert(0, "dropdown_option_enter_manual")
            if "dropdown_option_add_button" in selector_lower:
                keys.insert(0, "dropdown_option_add_button")
            if any(token in selector_lower for token in ("nav-search-submit", "search-submit", "search button")):
                keys.insert(0, "amazon_search_submit")
            if any(
                token in selector_lower
                for token in ("s-search-result", "h2 a", "product-image", "a-link-normal")
            ):
                keys.insert(0, "amazon_first_result")
            if any(token in selector_lower for token in ("add-to-cart", "add to cart", "submit.add-to-cart")):
                keys.insert(0, "amazon_add_to_cart")
            if any(token in selector_lower for token in ("nav-cart", "cart")):
                keys.insert(0, "amazon_cart")
        if step_type == "drag":
            if any(
                token in selector_lower
                for token in ("short answer", "short_answer", "shortanswer")
            ):
                keys.insert(0, "short_answer_source")
            if any(token in selector_lower for token in ("email", "field-email")):
                keys.insert(0, "email_field_source")
            if any(token in selector_lower for token in ("dropdown", "linked dropdown", "field-dropdown")):
                keys.insert(0, "dropdown_field_source")
            if any(
                token in selector_lower
                for token in ("canvas", "dropzone", "drop zone", "form-canvas", "form builder")
            ):
                keys.insert(0, "form_canvas_target")
        if step_type == "verify_text":
            hint_lower = (text_hint or "").lower()
            if any(token in hint_lower for token in ("create form", "create_form", "createform")):
                keys.insert(0, "create_form")
            if any(token in hint_lower for token in ("login", "sign in", "signin", "log in")):
                keys.insert(0, "login_button")
            if any(token in hint_lower for token in ("save", "save form", "save_form")):
                keys.insert(0, "save_form")
            if "create form" in selector_lower or "create_form" in selector_lower or "createform" in selector_lower:
                keys.insert(0, "create_form")
            if "form_list_first_row" in selector_lower:
                keys.insert(0, "form_list_first_row")
        if step_type == "wait":
            if "dropdown_options_section" in selector_lower:
                keys.insert(0, "dropdown_options_section")

        ordered_keys = self._dedupe(keys)
        candidates: list[str] = []
        strict_dropdown_keys = {
            "dropdown_option_type_trigger",
            "dropdown_option_enter_manual",
            "dropdown_options_section",
            "dropdown_option_label",
            "dropdown_option_value",
            "dropdown_option_add_button",
        }
        for key in ordered_keys:
            profile_candidates = self._merge_profile_candidates(key, selector_profile)
            for candidate in profile_candidates:
                normalized = self._apply_template(candidate, test_data).strip()
                if normalized:
                    candidates.append(normalized)
            # For brittle dropdown modal actions, do not let stale selector-memory
            # candidates override exact profile selectors.
            if key not in strict_dropdown_keys:
                candidates.extend(self._memory_candidates(run_domain, step_type, key))

        if not alias_key:
            candidates.extend(self._memory_candidates(run_domain, step_type, selector))
            candidates.append(selector)
            candidates.extend(self._derive_selector_variants(selector, step_type))

        deduped = self._dedupe(candidates)
        if step_type == "drag":
            deduped = self._prioritize_drag_candidates(deduped, alias_key=alias_key)
        effective_filter_key = alias_key
        if not effective_filter_key and step_type == "type":
            if "email" in selector_lower:
                effective_filter_key = "email"
            elif "password" in selector_lower:
                effective_filter_key = "password"
            elif "dropdown_option_label" in selector_lower:
                effective_filter_key = "dropdown_option_label"
            elif "dropdown_option_value" in selector_lower:
                effective_filter_key = "dropdown_option_value"
        if effective_filter_key:
            deduped = self._filter_alias_candidates(effective_filter_key, deduped)
        return deduped

    @staticmethod
    def _prioritize_drag_candidates(candidates: list[str], alias_key: str | None) -> list[str]:
        key = (alias_key or "").strip().lower()

        def score(selector: str) -> int:
            s = selector.lower()
            value = 100
            if key == "short_answer_source":
                if "[data-testid='field-short-answer']" in s:
                    value -= 90
                if "[data-testid*='short-answer']" in s:
                    value -= 82
                if "[data-rbd-draggable-id*='short']" in s:
                    value -= 78
                if "[draggable='true']" in s:
                    value -= 75
                if "[role='listitem']" in s:
                    value -= 60
                if "button:has-text('short answer')" in s or "[role='button']:has-text('short answer')" in s:
                    value -= 35
                if "text=short answer" in s:
                    value -= 25
            if key == "form_canvas_target":
                if "[data-row-id].form-row[draggable='true']" in s:
                    value -= 95
                if "[data-row-id]" in s:
                    value -= 90
                if "[data-testid='form-builder-canvas']" in s:
                    value -= 85
                if ".form-canvas" in s or ".form-drop-area" in s or ".form-builder-canvas" in s:
                    value -= 70
                if "[data-testid='form-canvas']" in s or "[class*='drop'][class*='canvas']" in s:
                    value -= 55
                if "div.form-row[draggable='true']:has-text('drag and drop fields here')" in s:
                    value -= 25
                if "div.form-row.relative.flex.w-full[draggable='true']:has-text('drag and drop fields here')" in s:
                    value -= 22
                if "drag and drop fields here" in s:
                    value += 15
                if "[role='application']" in s:
                    value += 25
            if key == "email_field_source":
                if "[draggable='true']" in s:
                    value -= 90
                if "[role='listitem']" in s:
                    value -= 75
                if "[data-rbd-draggable-id*='email']" in s:
                    value -= 70
                if "[data-testid='field-email']" in s:
                    value -= 55
                if "[data-testid*='field-email']" in s:
                    value -= 50
                if "button:has-text('email')" in s or "[role='button']:has-text('email')" in s:
                    value -= 40
                if "text=email" in s:
                    value -= 20
            return value

        return sorted(candidates, key=score)

    def _filter_alias_candidates(self, alias_key: str, candidates: list[str]) -> list[str]:
        key = alias_key.strip().lower()
        if key == "dropdown_option_enter_manual":
            filtered = [
                c for c in candidates
                if "enter options manually" in c.lower() and "use a saved list" not in c.lower()
            ]
            return filtered or candidates

        if key == "dropdown_option_label":
            filtered = [
                c for c in candidates
                if ("placeholder='label'" in c.lower() or 'placeholder="label"' in c.lower() or "name='label'" in c.lower() or 'name="label"' in c.lower())
                and "enter a label" not in c.lower()
            ]
            return filtered or candidates

        if key == "dropdown_option_value":
            filtered = [
                c for c in candidates
                if "placeholder='value'" in c.lower() or 'placeholder="value"' in c.lower() or "name='value'" in c.lower() or 'name="value"' in c.lower()
            ]
            return filtered or candidates

        if key == "dropdown_option_type_trigger":
            filtered = [
                c for c in candidates
                if "select an option" in c.lower() or "option type" in c.lower() or "[role='combobox']" in c.lower()
            ]
            return filtered or candidates

        if key == "dropdown_option_add_button":
            preferred_markers = (
                "add-option",
                "aria-label*='add option'",
                "title*='add option'",
                "svg[class*='plus']",
                "input[placeholder='value']) button",
                ":has-text('+')",
                "text=+",
            )
            filtered = [c for c in candidates if any(m in c.lower() for m in preferred_markers)]
            return filtered or candidates

        if key == "form_label":
            blocked_tokens = (
                "#formname",
                "input#formname",
                "input[name='formname']",
                "input[name='name']",
                "textarea[name='name']",
                "placeholder*='name'",
                "placeholder=\"name\"",
                "placeholder='name'",
                "input[type='text']",
            )
            filtered = [
                candidate
                for candidate in candidates
                if not any(token in candidate.lower() for token in blocked_tokens)
            ]
            return filtered or candidates

        if key == "email":
            # Prevent cross-field leakage from selector memory.
            blocked_tokens = (
                "#password",
                "name='password'",
                "name=\"password\"",
                "type='password'",
                "type=\"password\"",
            )
            filtered = [
                candidate
                for candidate in candidates
                if not any(token in candidate.lower() for token in blocked_tokens)
            ]
            return filtered or candidates

        if key == "password":
            blocked_tokens = (
                "#username",
                "name='username'",
                "name=\"username\"",
                "type='email'",
                "type=\"email\"",
                "autocomplete='email'",
                "autocomplete=\"email\"",
            )
            filtered = [
                candidate
                for candidate in candidates
                if not any(token in candidate.lower() for token in blocked_tokens)
            ]
            return filtered or candidates

        return candidates

    async def _run_with_drag_fallback(
        self,
        *,
        raw_source_selector: str,
        raw_target_selector: str,
        selector_profile: dict[str, list[str]],
        test_data: dict[str, Any],
        run_domain: str | None,
        target_offset_x: int | None = None,
        target_offset_y: int | None = None,
    ) -> str:
        is_vitaone_domain = bool(run_domain and "vitaone.io" in run_domain.lower())
        target_seed = raw_target_selector
        if is_vitaone_domain and "drag and drop fields here" in raw_target_selector.lower():
            # Avoid stale placeholder target after first drop.
            target_seed = "form_canvas_target"

        source_candidates = self._selector_candidates(
            raw_source_selector,
            "drag",
            selector_profile,
            test_data,
            run_domain,
        )
        target_candidates = self._selector_candidates(
            target_seed,
            "drag",
            selector_profile,
            test_data,
            run_domain,
        )
        if not source_candidates:
            raise ValueError(f"No drag source selector candidates for: {raw_source_selector}")
        if not target_candidates:
            raise ValueError(f"No drag target selector candidates for: {raw_target_selector}")

        last_error: Exception | None = None
        attempts: list[str] = []
        source_base = source_candidates[:6]
        target_base = target_candidates[:5]

        source_text_candidate = next((candidate for candidate in source_candidates if candidate.startswith("text=")), None)
        target_placeholder_candidate = next(
            (candidate for candidate in target_candidates if "Drag and drop fields here" in candidate),
            None,
        )

        source_pool = list(source_base)
        target_pool = list(target_base)
        if source_text_candidate and source_text_candidate not in source_pool:
            source_pool.append(source_text_candidate)
        if target_placeholder_candidate and target_placeholder_candidate not in target_pool:
            target_pool.append(target_placeholder_candidate)

        if is_vitaone_domain:
            # Second+ drags should always target stable canvas selectors, not placeholder text.
            target_pool = [
                candidate
                for candidate in target_pool
                if "drag and drop fields here" not in candidate.lower()
            ] or target_pool

            # For email field, prefer direct text/has-text selectors over aria-label variants.
            if "email" in raw_source_selector.lower():
                email_prioritized: list[str] = []
                for candidate in source_pool:
                    lower_candidate = candidate.lower()
                    if ":has-text('email')" in lower_candidate or "text=email" in lower_candidate:
                        email_prioritized.append(candidate)
                for candidate in source_pool:
                    if candidate not in email_prioritized:
                        email_prioritized.append(candidate)
                source_pool = email_prioritized

        primary_targets = target_pool[:2] if len(target_pool) >= 2 else target_pool
        pair_set: set[tuple[str, str]] = set()
        pairs: list[tuple[str, str]] = []

        # Phase 1: quickly validate multiple source candidates against primary targets.
        for source_selector in source_pool:
            for target_selector in primary_targets:
                pair = (source_selector, target_selector)
                if pair in pair_set:
                    continue
                pair_set.add(pair)
                pairs.append(pair)
                if len(pairs) >= 6:
                    break
            if len(pairs) >= 6:
                break

        # Phase 2: then widen to more combinations.
        if len(pairs) < 6:
            for source_selector in source_pool:
                for target_selector in target_pool:
                    pair = (source_selector, target_selector)
                    if pair in pair_set:
                        continue
                    pair_set.add(pair)
                    pairs.append(pair)
                    if len(pairs) >= 6:
                        break
                if len(pairs) >= 6:
                    break

        if not pairs:
            raise ValueError("No drag selector pairs available")

        # VitaOne builder drag is sensitive; repeated multi-pair retries can cause
        # duplicate drag actions even after a successful visual drop. Keep
        # attempts bounded but allow more than one source candidate.
        if is_vitaone_domain:
            pairs = pairs[:3]

        recovery_attempts = max(1, min(self._selector_recovery_attempts(), 2))
        if is_vitaone_domain:
            recovery_attempts = 1
        step_timeout = max(float(getattr(self._settings, "step_timeout_seconds", 60)), 5.0)
        # Drag/drop UIs often need a longer interaction window than click/type.
        step_budget_s = max(20.0, step_timeout * 0.90)
        # Drag adapters already perform internal multi-strategy retries; give each
        # selector pair more time instead of spreading time across many pairs.
        effective_pair_budget = max(min(len(pairs), 2) * recovery_attempts, 1)
        pair_timeout_s = min(35.0, max(15.0, step_budget_s / effective_pair_budget))
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        budget_exhausted = False

        for cycle in range(recovery_attempts):
            for source_selector, target_selector in pairs:
                    elapsed = loop.time() - started_at
                    if elapsed >= step_budget_s:
                        last_error = TimeoutError(
                            f"drag budget exceeded after {elapsed:.1f}s "
                            f"(pairs={len(pairs)}, attempts={len(attempts)})"
                        )
                        budget_exhausted = True
                        break
                    try:
                        async def _invoke_drag() -> str:
                            try:
                                return await self._browser.drag_and_drop(
                                    source_selector,
                                    target_selector,
                                    target_offset_x=target_offset_x,
                                    target_offset_y=target_offset_y,
                                )
                            except TypeError as te:
                                # Backward compatibility for test doubles / older adapters.
                                message = str(te)
                                if "unexpected keyword argument" not in message:
                                    raise
                                return await self._browser.drag_and_drop(source_selector, target_selector)

                        result = await asyncio.wait_for(
                            _invoke_drag(),
                            timeout=pair_timeout_s,
                        )
                        self._remember_selector_success(
                            run_domain=run_domain,
                            step_type="drag",
                            raw_selector=raw_source_selector,
                            resolved_selector=source_selector,
                            text_hint=None,
                        )
                        self._remember_selector_success(
                            run_domain=run_domain,
                            step_type="drag",
                            raw_selector=raw_target_selector,
                            resolved_selector=target_selector,
                            text_hint=None,
                        )
                        return result
                    except Exception as exc:
                        last_error = exc
                        compact_error = self._compact_error(exc).lower()
                        timeout_like = isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or (
                            "timeout" in compact_error
                        )
                        if is_vitaone_domain and timeout_like:
                            drag_label = self._extract_drag_label_from_selector(
                                raw_source_selector
                            ) or self._extract_drag_label_from_selector(source_selector)
                            if drag_label:
                                try:
                                    await asyncio.wait_for(
                                        self._browser.verify_text(
                                            selector="[data-row-id], [data-testid='form-builder-canvas'], .form-canvas, .form-drop-area, div[role='dialog']",
                                            match="contains",
                                            value=drag_label,
                                        ),
                                        timeout=min(pair_timeout_s, 4.0),
                                    )
                                    self._remember_selector_success(
                                        run_domain=run_domain,
                                        step_type="drag",
                                        raw_selector=raw_source_selector,
                                        resolved_selector=source_selector,
                                        text_hint=None,
                                    )
                                    self._remember_selector_success(
                                        run_domain=run_domain,
                                        step_type="drag",
                                        raw_selector=raw_target_selector,
                                        resolved_selector=target_selector,
                                        text_hint=None,
                                    )
                                    return (
                                        f"Dragged {source_selector} to {target_selector} "
                                        "(executor post-timeout success check)"
                                    )
                                except Exception:
                                    pass
                            # VitaOne often opens an edit dialog immediately after a successful drop.
                            # If label editor is visible, treat timeout as recovered success.
                            try:
                                await asyncio.wait_for(
                                    self._browser.verify_text(
                                        selector="div[role='dialog'], [role='dialog'] input[placeholder='Enter a label'], [role='dialog'] button:has-text('Save')",
                                        match="contains",
                                        value="Save",
                                    ),
                                    timeout=min(pair_timeout_s, 3.0),
                                )
                                self._remember_selector_success(
                                    run_domain=run_domain,
                                    step_type="drag",
                                    raw_selector=raw_source_selector,
                                    resolved_selector=source_selector,
                                    text_hint=None,
                                )
                                self._remember_selector_success(
                                    run_domain=run_domain,
                                    step_type="drag",
                                    raw_selector=raw_target_selector,
                                    resolved_selector=target_selector,
                                    text_hint=None,
                                )
                                return (
                                    f"Dragged {source_selector} to {target_selector} "
                                    "(executor dialog-visible success check)"
                                )
                            except Exception:
                                pass
                        attempts.append(
                            "pass "
                            f"{cycle + 1}: {source_selector} -> {target_selector} "
                            f"(offset={target_offset_x},{target_offset_y}) -> {self._compact_error(exc)}"
                        )
            if budget_exhausted:
                break

            if cycle >= recovery_attempts - 1:
                break
            if not self._should_retry_selector_error(last_error):
                break
            await self._selector_recovery_pause()

        if last_error:
            attempted = "; ".join(attempts[:8])
            suffix = " ..." if len(attempts) > 8 else ""
            raise ValueError(f"All drag selector pairs failed: {attempted}{suffix}") from last_error
        raise ValueError("Drag step failed with no selector attempts")

    def _selector_recovery_attempts(self) -> int:
        if not bool(getattr(self._settings, "selector_recovery_enabled", True)):
            return 1
        configured = int(getattr(self._settings, "selector_recovery_attempts", 2))
        return max(configured, 1)

    async def _selector_recovery_pause(self) -> None:
        delay_ms = int(getattr(self._settings, "selector_recovery_delay_ms", 350))
        if delay_ms <= 0:
            return
        await asyncio.sleep(delay_ms / 1000)

    def _should_retry_selector_error(self, error: Exception | None) -> bool:
        if error is None:
            return False
        if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
            return True

        text = self._compact_error(error).lower()
        transient_markers = (
            "timeout",
            "waiting for",
            "element is not attached",
            "element is not visible",
            "element is outside of the viewport",
            "element is obscured",
            "intercept",
            "strict mode violation",
            "execution context was destroyed",
            "target closed",
            "navigation",
            "another element would receive the click",
        )
        return any(marker in text for marker in transient_markers)

    def _derive_selector_variants(self, selector: str, step_type: str) -> list[str]:
        variants: list[str] = []

        contains_match = re.search(r":contains\((['\"])(.*?)\1\)", selector)
        if contains_match:
            contains_text = contains_match.group(2).strip()
            has_text_selector = re.sub(
                r":contains\((['\"])(.*?)\1\)",
                lambda _: f':has-text("{self._escape_playwright_text(contains_text)}")',
                selector,
                count=1,
            )
            variants.append(has_text_selector)
            if contains_text and step_type in {"click", "verify_text"}:
                variants.append(f"text={contains_text}")

        variants.extend(self._id_case_variants(selector))

        if ":first-child" in selector:
            variants.append(selector.replace(":first-child", ""))
        if ":nth-child(1)" in selector:
            variants.append(selector.replace(":nth-child(1)", ""))

        selector_lower = selector.lower()
        if "s-main-slot" in selector_lower or "s-search-result" in selector_lower:
            variants.extend(
                [
                    "div[data-component-type='s-search-result'] h2 a",
                    "h2 a.a-link-normal",
                    "h2 a",
                ]
            )
        if "h2 a:visible" in selector_lower:
            variants.extend(
                [
                    "div[data-component-type='s-search-result'] h2 a",
                    "h2 a.a-link-normal",
                    "h2 a",
                ]
            )

        return self._dedupe(variants)

    def _id_case_variants(self, selector: str) -> list[str]:
        id_match = re.search(r"#([A-Za-z][A-Za-z0-9_-]*)", selector)
        if not id_match:
            return []

        identifier = id_match.group(1)
        variants: list[str] = []
        if "_" in identifier:
            camel = self._snake_to_camel(identifier)
            if camel and camel != identifier:
                variants.append(selector.replace(f"#{identifier}", f"#{camel}", 1))
        if any(char.isupper() for char in identifier):
            snake = self._camel_to_snake(identifier)
            if snake and snake != identifier:
                variants.append(selector.replace(f"#{identifier}", f"#{snake}", 1))

        return variants

    @staticmethod
    def _snake_to_camel(value: str) -> str:
        parts = [part for part in value.split("_") if part]
        if not parts:
            return value
        return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])

    @staticmethod
    def _camel_to_snake(value: str) -> str:
        return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()

    @staticmethod
    def _escape_playwright_text(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _selector_alias_key(selector: str) -> str | None:
        text = selector.strip()
        alias_patterns = (
            r"^\{\{\s*selector\.([a-zA-Z0-9_.-]+)\s*\}\}$",
            r"^\$([a-zA-Z0-9_.-]+)$",
            r"^profile:([a-zA-Z0-9_.-]+)$",
        )
        for pattern in alias_patterns:
            match = re.match(pattern, text)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _merge_profile_candidates(key: str, selector_profile: dict[str, list[str]]) -> list[str]:
        values: list[str] = []
        values.extend(selector_profile.get(key, []))
        values.extend(DEFAULT_SELECTOR_PROFILE.get(key, []))
        deduped: list[str] = []
        seen: set[str] = set()
        for item in values:
            token = item.strip()
            if not token or token in seen:
                continue
            seen.add(token)
            deduped.append(token)
        return deduped

    def _apply_template(self, text: str, test_data: dict[str, Any]) -> str:
        if not text or "{{" not in text:
            return text

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            builtin = self._resolve_builtin_template(key)
            if builtin is not None:
                return builtin
            value = self._lookup_test_data_value(key, test_data)
            if value is None:
                return match.group(0)
            return str(value)

        return TEMPLATE_PATTERN.sub(replace, text)

    def _resolve_builtin_template(self, key: str) -> str | None:
        token = key.strip()
        if not token:
            return None

        upper = token.upper()
        now = datetime.now()

        if upper in {"NOW", "TIMESTAMP", "CURRENT_TIMESTAMP"}:
            return now.strftime("%Y-%m-%d_%H-%M-%S")

        if upper == "UUID":
            return str(uuid4())

        if upper.startswith("NOW_"):
            fmt = self._convert_now_format(token[4:])
            if fmt:
                return now.strftime(fmt)

        if token.startswith("now:") or token.startswith("NOW:"):
            raw_fmt = token.split(":", 1)[1].strip()
            if raw_fmt:
                return now.strftime(raw_fmt)

        return None

    @staticmethod
    def _convert_now_format(token: str) -> str:
        text = token.strip()
        if not text:
            return ""

        special = {
            "YYYYMMDD_HHMMSS": "%Y%m%d_%H%M%S",
            "YYYYMMDDHHMMSS": "%Y%m%d%H%M%S",
        }
        if text in special:
            return special[text]

        result = text
        result = result.replace("HHMMSS", "%H%M%S")
        result = result.replace("HHMM", "%H%M")
        result = result.replace("YYYY", "%Y")
        result = result.replace("YY", "%y")
        result = result.replace("MM", "%m")
        result = result.replace("DD", "%d")
        result = result.replace("HH", "%H")
        result = result.replace("mm", "%M")
        result = result.replace("SS", "%S")
        result = result.replace("ss", "%S")
        return result

    @staticmethod
    def _lookup_test_data_value(key: str, test_data: dict[str, Any]) -> Any:
        if key in test_data:
            return test_data[key]

        target = key.lower()
        for existing_key, existing_value in test_data.items():
            if existing_key.lower() == target:
                return existing_value
        return None

    @staticmethod
    def _dedupe(values: list[str]) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for item in values:
            normalized = item.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        return ordered

    @staticmethod
    def _extract_drag_label_from_selector(selector: str) -> str | None:
        text = selector.strip()
        lowered = text.lower()
        if any(token in lowered for token in ("short answer", "short-answer", "short_answer", "field-short")):
            return "Short answer"
        if any(token in lowered for token in ("field-email", "email")):
            return "Email"
        if any(token in lowered for token in ("field-dropdown", "linked dropdown", "dropdown")):
            return "Dropdown"

        has_text = re.search(r":has-text\((['\"])(.*?)\1\)", text, re.IGNORECASE)
        if has_text and has_text.group(2).strip():
            return has_text.group(2).strip()

        text_selector = re.search(r"^text\s*=\s*(.+)$", text, re.IGNORECASE)
        if text_selector and text_selector.group(1).strip():
            return text_selector.group(1).strip().strip("'\"")

        return None

    @staticmethod
    def _compact_error(exc: Exception) -> str:
        text = str(exc).strip().replace("\r", " ").replace("\n", " ")
        text = re.sub(r"\s+", " ", text)
        if not text:
            text = repr(exc)
        if text in {"Exception()", "RuntimeError()", "ValueError()", "TimeoutError()"}:
            text = f"{type(exc).__name__}: {repr(exc)}"
        if len(text) > 220:
            return f"{text[:217]}..."
        return text

    def _memory_candidates(self, run_domain: str | None, step_type: str, key: str) -> list[str]:
        store = self._selector_memory
        if not store or not run_domain:
            return []
        key_token = key.strip()
        if not key_token:
            return []
        max_items = max(int(getattr(self._settings, "selector_memory_max_candidates", 5)), 1)
        return store.get_candidates(run_domain, step_type, key_token, limit=max_items)

    def _remember_selector_success(
        self,
        *,
        run_domain: str | None,
        step_type: str,
        raw_selector: str,
        resolved_selector: str,
        text_hint: str | None,
    ) -> None:
        store = self._selector_memory
        if not store or not run_domain:
            return

        keys = [raw_selector.strip()]
        alias = self._selector_alias_key(raw_selector)
        if alias:
            keys.append(alias)

        selector_lower = raw_selector.lower()
        if step_type == "type":
            # Do not infer email key from "@" because passwords often contain it.
            if "email" in selector_lower:
                keys.extend(["email", "username"])
            if "password" in selector_lower or (text_hint and "password" in text_hint.lower()):
                keys.append("password")
            if "formname" in selector_lower or "form name" in selector_lower or "qa_form" in (text_hint or "").lower():
                keys.append("form_name")
            if "label" in selector_lower or "first name" in (text_hint or "").lower():
                keys.append("form_label")
            if any(token in selector_lower for token in ("twotabsearchtextbox", "field-keywords")):
                keys.append("amazon_search_box")
        if step_type in {"click", "verify_text"}:
            if any(token in selector_lower for token in ("create form", "create_form", "createform")):
                keys.append("create_form")
            if any(token in selector_lower for token in ("save form", "save_form", "saveform")):
                keys.append("save_form")
            if any(token in selector_lower for token in ("back button", "selector.back_button")):
                keys.append("back_button")
            if any(token in selector_lower for token in ("required", "checkbox")):
                keys.append("required_checkbox")
            if any(token in selector_lower for token in ("login", "sign in", "signin", "log in")):
                keys.append("login_button")
            if any(token in selector_lower for token in ("nav-search-submit", "search-submit", "search button")):
                keys.append("amazon_search_submit")
            if any(
                token in selector_lower
                for token in ("s-search-result", "h2 a", "product-image", "a-link-normal")
            ):
                keys.append("amazon_first_result")
            if any(token in selector_lower for token in ("add-to-cart", "add to cart", "submit.add-to-cart")):
                keys.append("amazon_add_to_cart")
            if any(token in selector_lower for token in ("nav-cart", "cart")):
                keys.append("amazon_cart")
        if step_type == "drag":
            if any(
                token in selector_lower
                for token in ("short answer", "short_answer", "shortanswer")
            ):
                keys.append("short_answer_source")
            if any(token in selector_lower for token in ("email", "field-email")):
                keys.append("email_field_source")
            if any(token in selector_lower for token in ("dropdown", "linked dropdown", "field-dropdown")):
                keys.append("dropdown_field_source")
            if any(
                token in selector_lower
                for token in ("canvas", "dropzone", "drop zone", "form-canvas", "form builder")
            ):
                keys.append("form_canvas_target")

        for key in self._dedupe(keys):
            store.remember_success(run_domain, step_type, key, resolved_selector)

    @staticmethod
    def _extract_run_domain(run: RunState) -> str | None:
        candidate_urls: list[str] = []
        if run.start_url:
            candidate_urls.append(run.start_url)
        for step in run.steps:
            if step.type == "navigate":
                raw_url = step.input.get("url")
                if isinstance(raw_url, str):
                    candidate_urls.append(raw_url)

        for raw_url in candidate_urls:
            try:
                parsed = urlparse(raw_url)
            except Exception:
                continue
            domain = (parsed.netloc or "").strip().lower()
            if domain:
                return domain
        return None

    @staticmethod
    def _build_summary(run) -> str:
        completed = sum(1 for step in run.steps if step.status == StepStatus.completed)
        failed = sum(1 for step in run.steps if step.status == StepStatus.failed)
        cancelled = sum(1 for step in run.steps if step.status == StepStatus.cancelled)
        return (
            f"Run '{run.run_name}' ended with status {run.status}. "
            f"Completed={completed}, Failed={failed}, Cancelled={cancelled}."
        )
