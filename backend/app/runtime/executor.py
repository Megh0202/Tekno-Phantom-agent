from __future__ import annotations
import asyncio
import contextlib
from contextvars import ContextVar
from pathlib import Path
from dataclasses import dataclass
from html import escape
import json
import logging
import re
from types import SimpleNamespace
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from time import perf_counter
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from app.brain.base import BrainClient
from app.config import Settings
from app.mcp.browser_client import BrowserMCPClient
from app.mcp.filesystem_client import FileSystemClient
from app.runtime.plan_normalizer import normalize_plan_steps
from app.runtime.perception import (
    ElementIndex,
    PerceptionMatch,
    build_element_index,
    find_best_match,
    find_by_signatures,
)
from app.runtime.selector_memory import SelectorMemoryStore
from app.runtime.store import RunStore
from app.schemas import RunState, RunStatus, StepRuntimeState, StepStatus

LOGGER = logging.getLogger("tekno.phantom.executor")
TEMPLATE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")
DEFAULT_SELECTOR_PROFILE: dict[str, list[str]] = {
    "popup_accept": [
        "button:has-text('Accept')",
        "button:has-text('Accept all')",
        "button:has-text('I agree')",
        "button:has-text('Agree')",
        "button:has-text('Allow all')",
        "button:has-text('Allow')",
        "button:has-text('Continue')",
        "button:has-text('OK')",
        "button:has-text('Got it')",
        "button:has-text('Alle akzeptieren')",
        "button:has-text('Akzeptieren')",
        "button:has-text('Zustimmen')",
        "[role='button']:has-text('Accept')",
        "[role='button']:has-text('Accept all')",
        "[role='button']:has-text('Alle akzeptieren')",
        "[role='button']:has-text('Akzeptieren')",
        "[id*='accept']",
        "[data-testid*='accept']",
        "[aria-label*='accept']",
        "[aria-label*='cookie']",
    ],
    "popup_dismiss": [
        "button[aria-label*='Close']",
        "button[aria-label*='Dismiss']",
        "button[aria-label*='Schlie']",
        "button:has-text('Close')",
        "button:has-text('Dismiss')",
        "button:has-text('Skip')",
        "button:has-text('Not now')",
        "button:has-text('Later')",
        "button:has-text('Schlie')",
        "[role='button']:has-text('Close')",
        "[role='button']:has-text('Dismiss')",
        "[data-testid*='close']",
        "[aria-label*='close']",
    ],
    "email": [
        "#username",
        "input[name='username']",
        "input[name='email']",
        "input[id='email']",
        "input[type='email']",
        "input[autocomplete='email']",
        "input[autocomplete='username']",
        "input[placeholder*='Email']",
        "input[placeholder*='email']",
        "input[type='text']",
    ],
    "username": [
        "#username",
        "input[name='username']",
        "input[name='email']",
        "input[id='email']",
        "input[placeholder*='Email']",
        "input[placeholder*='email']",
        "input[type='text']",
    ],
    "password": [
        "#password",
        "input[name='password']",
        "input[id='password']",
        "input[type='password']",
        "input[placeholder*='Password']",
        "input[placeholder*='password']",
    ],
    "confirm_password": [
        "input[name='confirm_password']",
        "input[name='confirmPassword']",
        "input[id='confirm_password']",
        "input[id='confirmPassword']",
        "input[placeholder*='Confirm Password']",
        "input[placeholder*='confirm password']",
        "input[autocomplete='new-password']",
    ],
    "first_name": [
        "input[name='firstName']",
        "input[name='first_name']",
        "input[id='firstName']",
        "input[id='first_name']",
        "input[name='name']",
        "input[id='name']",
        # Placeholder-based — must NOT be inside a phone-widget wrapper
        "input[placeholder='First Name']:not(.iti__tel-input):not(.PhoneInputInput)",
        "input[placeholder*='First Name']:not(.iti__tel-input):not(.PhoneInputInput)",
        "input[placeholder*='first name']:not(.iti__tel-input):not(.PhoneInputInput)",
        "input[aria-label*='First Name']:not(.iti__tel-input):not(.PhoneInputInput)",
        "label:has-text('First Name') + input",
        "label:has-text('First Name') ~ input",
    ],
    "surname": [
        "input[name='surname']",
        "input[name='lastName']",
        "input[name='last_name']",
        "input[id='surname']",
        "input[id='lastName']",
        "input[placeholder='Surname']",
        "input[placeholder*='Surname']",
        "input[placeholder='Last Name']",
        "input[placeholder*='Last Name']",
        "input[aria-label*='Surname']",
        "input[aria-label*='Last Name']",
        "label:has-text('Surname') + input",
        "label:has-text('Surname') ~ input",
        "label:has-text('Last Name') + input",
        "label:has-text('Last Name') ~ input",
    ],
    "email": [
        "input[type='email']",
        "input[name='email']",
        "input[id='email']",
        "input[autocomplete='email']",
        "input[placeholder='Email Address']",
        "input[placeholder*='Email']",
        "input[aria-label*='Email']",
        "label:has-text('Email') + input",
        "label:has-text('Email') ~ input",
    ],
    "phone": [
        # Named / typed attributes (most reliable)
        "input[name='phone']",
        "input[name='mobile']",
        "input[name='phone_number']",
        "input[name='phoneNumber']",
        "input[id='phone']",
        "input[id='mobile']",
        "input[type='tel']",
        "input[autocomplete='tel']",
        # intl-tel-input library (flag + code picker) — the actual number input
        "input.iti__tel-input",
        ".iti input[type='tel']",
        ".iti input[type='text']",
        ".iti__flag-container ~ input",
        # react-phone-number-input
        "input.PhoneInputInput",
        ".PhoneInput input",
        # react-phone-input-2
        "input.form-control[placeholder]",
        # Generic placeholder fallback
        "input[placeholder*='Phone']",
        "input[placeholder*='Mobile']",
        "input[placeholder*='phone']",
    ],
    "next_button": [
        "button:has-text('Next')",
        "[role='button']:has-text('Next')",
        "button:has-text('NEXT')",
        "[role='button']:has-text('NEXT')",
        "text=Next",
    ],
    "create_account": [
        "button:has-text('Create Account')",
        "[role='button']:has-text('Create Account')",
        "button[type='submit']",
        "input[type='submit']",
        "text=Create Account",
    ],
    "logout_link": [
        "a:has-text('Logout')",
        "button:has-text('Logout')",
        "[role='menuitem']:has-text('Logout')",
        "[aria-label*='logout']",
        "text=Logout",
    ],
    "login_button": [
        "button[name='login']",
        "button:has-text('Log In')",
        "button:has-text('Login')",
        "button:has-text('Sign In')",
        "[role='button']:has-text('Log In')",
        "[role='button']:has-text('Login')",
        "button[type='submit']",
        "input[type='submit']",
        "text=Sign In",
        "text=Log In",
        "text=Login",
    ],
    "language_switcher": [
        "button[aria-label*='language']",
        "[role='button'][aria-label*='language']",
        "button[title*='language']",
        "[title*='language']",
        "[aria-haspopup='listbox']",
        "[role='combobox']",
        "select[name*='lang']",
        "select[name*='locale']",
        "[name*='lang']",
        "[id*='lang']",
        "[data-testid*='lang']",
        "[data-testid*='locale']",
        "button:has-text('DE')",
        "button:has-text('EN')",
        "button:has-text('FR')",
        "button:has-text('ES')",
        "text=DE",
        "text=EN",
        "text=FR",
        "text=ES",
    ],
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class StepIntent:
    action: str
    element_type: str
    target_text: str | None
    ordinal: int | None
    scope_hint: str | None
    raw_selector: str | None = None
    text_hint: str | None = None


@dataclass(frozen=True)
class CandidateConfidence:
    level: str
    top_score: int
    second_score: int | None
    score_gap: int
    retained_count: int


@dataclass(frozen=True)
class GroundedCandidate:
    score: int
    order: int
    selectors: list[str]
    element: dict[str, Any]


class _FastPathEscalation(Exception):
    pass


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
        self._run_context: dict[str, dict[str, Any]] = {}
        self._viewer_shutdown_tasks: dict[str, asyncio.Task[None]] = {}
        self._selector_timeout_tasks: dict[str, asyncio.Task[None]] = {}
        self._execution_locks: dict[str, asyncio.Lock] = {}
        self._step_trace_context: ContextVar[dict[str, Any] | None] = ContextVar(
            "executor_step_trace_context",
            default=None,
        )
        self._step_state_context: ContextVar[StepRuntimeState | None] = ContextVar(
            "executor_step_state_context",
            default=None,
        )

    async def execute(self, run_id: str) -> None:
        # Ensure only one execution runs at a time per run to prevent race conditions
        # when multiple background tasks (e.g. rapid selector submissions) call execute().
        if run_id not in self._execution_locks:
            self._execution_locks[run_id] = asyncio.Lock()
        async with self._execution_locks[run_id]:
            await self._execute_locked(run_id)

    async def _execute_locked(self, run_id: str) -> None:
        run = self._run_store.get(run_id)
        if not run:
            LOGGER.warning("execute() called for unknown run_id=%s", run_id)
            return

        is_new_run = run.started_at is None
        LOGGER.info(
            "Run %s: starting execution (mode=%s, steps=%d, url=%s)",
            run_id,
            run.execution_mode,
            len(run.steps),
            run.start_url or "",
        )
        run.test_data = self._initialize_runtime_test_data(run.test_data or {})
        run.status = RunStatus.running
        if is_new_run:
            run.started_at = utc_now()
        self._run_store.persist(run)
        preserve_browser_session = False

        try:
            await self._browser.start_run(run)
            self._sync_run_viewer_state(run)
            self._run_store.persist(run)

            should_navigate_to_start = False
            if run.start_url:
                if is_new_run:
                    should_navigate_to_start = True
                else:
                    snapshot = await self._safe_page_snapshot()
                    current_url = str((snapshot or {}).get("url") or "").strip().lower()
                    if current_url in {"", "about:blank", "chrome://newtab/", "edge://newtab/"}:
                        should_navigate_to_start = True

            if should_navigate_to_start and run.start_url:
                try:
                    await asyncio.wait_for(
                        self._browser.navigate(run.start_url),
                        timeout=self._settings.step_timeout_seconds,
                    )
                except Exception as exc:
                    await self._record_startup_navigation_failure(run, exc)
                    return

            has_step_failure = False
            if run.execution_mode == "autonomous" and run.prompt:
                has_step_failure = await self._execute_existing_steps(run)
                if run.status == RunStatus.running and not has_step_failure:
                    generated_failure = await self._execute_autonomous_run(run)
                    has_step_failure = has_step_failure or generated_failure
                if run.status == RunStatus.waiting_for_input:
                    preserve_browser_session = True
            else:
                has_step_failure = await self._execute_existing_steps(run)

            if run.status == RunStatus.waiting_for_input:
                preserve_browser_session = True

            if run.status == RunStatus.running:
                run.status = RunStatus.failed if has_step_failure else RunStatus.completed
                self._run_store.persist(run)
                LOGGER.info("Run %s: finished with status=%s", run_id, run.status.value)

            if run.status != RunStatus.waiting_for_input:
                summary_text = self._build_summary(run)
                run.summary = await self._brain.summarize(summary_text)
                if run.run_name.lower() not in (run.summary or "").lower():
                    run.summary = f"{run.summary} Run: {run.run_name}."
                await self._files.write_text_artifact(run_id, "summary.txt", run.summary)
                self._run_store.persist(run)
        except Exception as exc:
            run.status = RunStatus.failed
            run.summary = f"Run failed unexpectedly ({type(exc).__name__}): {exc!r}"
            if run.viewer_url and run.viewer_status in {None, "starting", "ready"}:
                run.viewer_status = "failed"
                run.viewer_last_error = self._compact_error(exc)
            self._run_store.persist(run)
            LOGGER.exception("Run %s failed unexpectedly", run_id)
        finally:
            run.finished_at = utc_now()
            if not preserve_browser_session:
                if self._should_delay_viewer_shutdown(run):
                    run.viewer_status = "closing_soon"
                    self._schedule_viewer_shutdown(run.run_id)
                else:
                    # Brief pause so user can see the final state before the browser closes.
                    await asyncio.sleep(5)
                    await self._browser.close_run(run_id)
                    self._mark_run_viewer_closed(run)
            elif run.status == RunStatus.waiting_for_input:
                # Browser is kept open — start a 40-second countdown.
                # If the user doesn't provide a selector in time, close the run.
                self._schedule_selector_input_timeout(run.run_id)
            self._run_store.persist(run)
            if not preserve_browser_session:
                await self._write_html_report(run)
            self._run_store.clear_cancel(run_id)

    def _sync_run_viewer_state(self, run: RunState) -> None:
        session = self._browser.get_viewer_session(run.run_id)
        if session is None:
            return
        run.viewer_token = session.token
        run.viewer_url = session.viewer_url
        run.viewer_status = session.status
        run.viewer_last_error = session.error

    @staticmethod
    def _mark_run_viewer_closed(run: RunState) -> None:
        if not run.viewer_url:
            return
        run.viewer_status = "closed"

    def _should_delay_viewer_shutdown(self, run: RunState) -> bool:
        delay_seconds = max(int(getattr(self._settings, "viewer_keepalive_seconds", 0)), 0)
        if delay_seconds <= 0:
            return False
        if not run.viewer_url:
            return False
        if run.viewer_status not in {"ready", "closing_soon"}:
            return False
        return run.status in {RunStatus.completed, RunStatus.failed, RunStatus.cancelled}

    def _schedule_viewer_shutdown(self, run_id: str) -> None:
        existing = self._viewer_shutdown_tasks.pop(run_id, None)
        if existing is not None:
            existing.cancel()

        task = asyncio.create_task(self._shutdown_viewer_after_grace_period(run_id))
        self._viewer_shutdown_tasks[run_id] = task

        def _cleanup_task(completed: asyncio.Task[None]) -> None:
            current = self._viewer_shutdown_tasks.get(run_id)
            if current is completed:
                self._viewer_shutdown_tasks.pop(run_id, None)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                completed.result()

        task.add_done_callback(_cleanup_task)

    async def _shutdown_viewer_after_grace_period(self, run_id: str) -> None:
        delay_seconds = max(int(getattr(self._settings, "viewer_keepalive_seconds", 0)), 0)
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        await self._browser.close_run(run_id)
        run = self._run_store.get(run_id)
        if run is None:
            return
        self._mark_run_viewer_closed(run)
        self._run_store.persist(run)

    _SELECTOR_INPUT_TIMEOUT_SECONDS: int = 40

    def _schedule_selector_input_timeout(self, run_id: str) -> None:
        existing = self._selector_timeout_tasks.pop(run_id, None)
        if existing is not None:
            existing.cancel()

        task = asyncio.create_task(self._expire_selector_input_wait(run_id))
        self._selector_timeout_tasks[run_id] = task

        def _cleanup(completed: asyncio.Task[None]) -> None:
            current = self._selector_timeout_tasks.get(run_id)
            if current is completed:
                self._selector_timeout_tasks.pop(run_id, None)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                completed.result()

        task.add_done_callback(_cleanup)
        LOGGER.info("Run %s: selector input timeout scheduled (%ds)", run_id, self._SELECTOR_INPUT_TIMEOUT_SECONDS)

    def _cancel_selector_input_timeout(self, run_id: str) -> None:
        task = self._selector_timeout_tasks.pop(run_id, None)
        if task is not None:
            task.cancel()
            LOGGER.info("Run %s: selector input timeout cancelled (user provided selector)", run_id)

    async def _expire_selector_input_wait(self, run_id: str) -> None:
        await asyncio.sleep(self._SELECTOR_INPUT_TIMEOUT_SECONDS)
        run = self._run_store.get(run_id)
        if run is None or run.status != RunStatus.waiting_for_input:
            return
        LOGGER.info(
            "Run %s: selector input timed out after %ds — marking step failed, continuing remaining steps",
            run_id, self._SELECTOR_INPUT_TIMEOUT_SECONDS,
        )

        # Mark the stuck waiting_for_input step as failed.
        # The execution loop in _execute_existing_steps is polling for this
        # status change and will continue to the next step automatically.
        for step in run.steps:
            if step.status == StepStatus.waiting_for_input:
                step.status = StepStatus.failed
                step.error = "Selector input timed out — no selector was provided in time."
                step.message = "Step failed: no selector provided within 40s. Continuing remaining steps."
                step.user_input_kind = None
                step.user_input_prompt = None
                step.requested_selector_target = None

        run.status = RunStatus.running
        self._run_store.persist(run)
        LOGGER.info(
            "Run %s: selector timeout — step marked failed, execution loop will continue remaining steps",
            run_id,
        )

    async def _record_startup_navigation_failure(self, run: RunState, exc: Exception) -> None:
        compact = self._compact_error(exc)
        if not compact.endswith("(step_type=navigate)"):
            compact = f"{compact} (step_type=navigate)"

        startup_step = StepRuntimeState(
            index=0,
            type="navigate",
            input={"type": "navigate", "url": run.start_url or ""},
            status=StepStatus.failed,
            message="Initial navigation failed",
            error=compact,
            started_at=utc_now(),
            ended_at=utc_now(),
        )
        for existing_step in run.steps:
            existing_step.index += 1
        run.steps.insert(0, startup_step)
        await self._capture_failure_screenshot(run.run_id, startup_step)

        run.status = RunStatus.failed
        run.summary = f"Initial navigation to {run.start_url or 'the start URL'} failed: {compact}"
        self._run_store.persist(run)
        LOGGER.error(
            "Run %s failed during startup navigation to %s: %s",
            run.run_id,
            run.start_url or "",
            compact,
        )

    async def _execute_existing_steps(self, run: RunState) -> bool:
        has_step_failure = False
        for step in run.steps:
            if step.status in {StepStatus.completed, StepStatus.skipped}:
                continue
            if step.status == StepStatus.waiting_for_input:
                if self._should_continue_after_failure(run):
                    # In continue mode: wait here until the timeout resolves the
                    # step (marks it failed) or the user provides a selector.
                    # Poll every second for up to the full timeout window.
                    run.status = RunStatus.waiting_for_input
                    self._run_store.persist(run)
                    for _ in range(self._SELECTOR_INPUT_TIMEOUT_SECONDS + 5):
                        await asyncio.sleep(1)
                        fresh = self._run_store.get(run.run_id)
                        if fresh is None:
                            break
                        current_status = next(
                            (s.status for s in fresh.steps if s.step_id == step.step_id),
                            step.status,
                        )
                        if current_status != StepStatus.waiting_for_input:
                            # Step resolved — sync state and continue loop
                            for s in fresh.steps:
                                for rs in run.steps:
                                    if rs.step_id == s.step_id:
                                        rs.status = s.status
                                        rs.message = s.message
                                        rs.error = s.error
                            break
                    # Only count as failure if the step did not complete successfully.
                    # A background-task retry that clicked and navigated must not
                    # poison the final run status.
                    if step.status != StepStatus.completed:
                        has_step_failure = True
                    run.status = RunStatus.running
                    continue
                else:
                    run.status = RunStatus.waiting_for_input
                    self._run_store.persist(run)
                    has_step_failure = True
                    break
            if self._run_store.is_cancelled(run.run_id):
                step.status = StepStatus.cancelled
                run.status = RunStatus.cancelled
                self._run_store.persist(run)
                break

            LOGGER.info(
                "Run %s: step %d/%d type=%s selector=%r",
                run.run_id,
                step.index + 1,
                len(run.steps),
                step.type,
                step.input.get("selector", "") if step.input else "",
            )
            await self._execute_step(run, step)
            LOGGER.info(
                "Run %s: step %d done status=%s message=%r",
                run.run_id,
                step.index + 1,
                step.status.value,
                (step.message or "")[:120],
            )
            self._run_store.persist(run)
            if step.status == StepStatus.waiting_for_input:
                if self._should_continue_after_failure(run):
                    run.status = RunStatus.waiting_for_input
                    self._run_store.persist(run)
                    # Start the 40s external timeout task so it marks the step
                    # failed in the store — the polling loop below detects that.
                    self._schedule_selector_input_timeout(run.run_id)
                    user_provided_selector = False
                    for _ in range(self._SELECTOR_INPUT_TIMEOUT_SECONDS + 5):
                        await asyncio.sleep(1)
                        fresh = self._run_store.get(run.run_id)
                        if fresh is None:
                            break
                        fresh_step = next(
                            (s for s in fresh.steps if s.step_id == step.step_id),
                            None,
                        )
                        if fresh_step is None:
                            break
                        if fresh_step.status == StepStatus.pending:
                            # User provided a selector — sync updated input and re-execute
                            step.status = fresh_step.status
                            step.input = dict(fresh_step.input or step.input)
                            step.message = fresh_step.message
                            step.error = fresh_step.error
                            step.provided_selector = fresh_step.provided_selector
                            user_provided_selector = True
                            break
                        if fresh_step.status != StepStatus.waiting_for_input:
                            # Timeout fired — step marked failed, move on
                            step.status = fresh_step.status
                            step.message = fresh_step.message
                            step.error = fresh_step.error
                            break
                    # Cancel external timeout task (may have already fired — that's fine).
                    self._cancel_selector_input_timeout(run.run_id)
                    # Safety net: if polling exhausted without any status change,
                    # mark step failed explicitly so it doesn't stay waiting_for_input.
                    if not user_provided_selector and step.status == StepStatus.waiting_for_input:
                        step.status = StepStatus.failed
                        step.error = "Selector input timed out — no selector was provided in time."
                        step.message = "Step failed: no selector provided within 40s."
                        step.user_input_kind = None
                        step.user_input_prompt = None
                        step.requested_selector_target = None
                        self._run_store.persist(run)
                    run.status = RunStatus.running
                    if user_provided_selector:
                        # Re-execute the step with the user's selector
                        LOGGER.info(
                            "Run %s: step %d — user provided selector, retrying",
                            run.run_id, step.index + 1,
                        )
                        await self._execute_step(run, step)
                        self._run_store.persist(run)
                        # Only count as a failure if the retry itself failed.
                        # A successful retry must not poison the final run status.
                        if step.status != StepStatus.completed:
                            has_step_failure = True
                    else:
                        # Timeout / no selector provided — step was already marked failed.
                        has_step_failure = True
                    continue
                else:
                    run.status = RunStatus.waiting_for_input
                    self._run_store.persist(run)
                    has_step_failure = True
                    break
            if step.status == StepStatus.failed:
                has_step_failure = True
                # Agentic fallback: only attempt when failure_mode is "continue".
                # When failure_mode is "stop" we want to halt immediately — the
                # fallback would inject a new step and `continue` the loop,
                # causing subsequent steps to run even though the run should pause.
                is_fallback_step = bool((step.input or {}).get("_is_fallback"))
                if run.prompt and not is_fallback_step and self._should_continue_after_failure(run):
                    injected = await self._try_agentic_fallback(run, step)
                    if injected:
                        continue
                if self._should_continue_after_failure(run):
                    continue
                self._mark_remaining_steps_skipped(run, step.index + 1)
                break
        return has_step_failure

    async def _execute_autonomous_run(self, run: RunState) -> bool:
        max_steps = max(int(self._settings.max_steps_per_run), 1)
        history: list[dict[str, Any]] = self._history_from_run(run)
        has_step_failure = False

        while len(run.steps) < max_steps:
            if self._run_store.is_cancelled(run.run_id):
                run.status = RunStatus.cancelled
                self._run_store.persist(run)
                break

            await self._auto_handle_known_popups(run)
            page_snapshot = await self._browser.inspect_page()
            memory_context = self._build_autonomous_memory(run)
            remaining_steps = max_steps - len(run.steps)
            decision = await self._brain.next_action(
                goal=run.prompt,
                page=page_snapshot,
                history=history,
                remaining_steps=remaining_steps,
                memory=memory_context,
            )

            decision_status = str(decision.get("status", "")).strip().lower()
            if decision_status == "complete":
                summary = str(decision.get("summary", "")).strip()
                if summary:
                    run.summary = summary
                break

            raw_action = decision.get("action")
            normalized_steps = normalize_plan_steps(
                [raw_action],
                max_steps=1,
                default_wait_ms=self._settings.planner_default_wait_ms,
            )
            if not normalized_steps:
                has_step_failure = True
                run.summary = "Autonomous mode stopped because the brain returned an invalid action."
                break

            step_input = normalized_steps[0]
            step = StepRuntimeState(
                index=len(run.steps),
                type=str(step_input.get("type", "step")),
                input=step_input,
                status=StepStatus.pending,
            )
            run.steps.append(step)
            self._run_store.persist(run)

            await self._execute_step(run, step)
            self._run_store.persist(run)
            history.append(
                {
                    "step_index": step.index,
                    "type": step.type,
                    "input": step.input,
                    "status": step.status.value,
                    "message": step.message,
                    "error": step.error,
                }
            )
            if step.status == StepStatus.waiting_for_input:
                self._run_store.persist(run)
                has_step_failure = True
                continue
            if step.status == StepStatus.failed:
                has_step_failure = True
                if self._should_continue_after_failure(run):
                    continue
                break

        if len(run.steps) >= max_steps and run.status == RunStatus.running:
            has_step_failure = True
            run.summary = "Autonomous mode reached the maximum step budget before completing the goal."

        return has_step_failure

    async def _try_agentic_fallback(
        self, run: RunState, failed_step: StepRuntimeState
    ) -> bool:
        """
        After a deterministic step fails, ask the brain what to do next.
        If the brain returns a valid action, append a new StepRuntimeState to
        run.steps (the for-loop in _execute_existing_steps will pick it up
        naturally) and return True.  Returns False if recovery is not possible.
        """
        if not run.prompt:
            return False
        try:
            page_snapshot = await self._safe_page_snapshot()
            history = self._history_from_run(run)
            max_steps = max(int(self._settings.max_steps_per_run), 1)
            remaining = max(max_steps - len(run.steps), 1)
            memory_context = self._build_autonomous_memory(run)

            LOGGER.info(
                "Run %s step %d: agentic fallback triggered (error=%r)",
                run.run_id, failed_step.index + 1, failed_step.error,
            )

            decision = await self._brain.next_action(
                goal=run.prompt,
                page=page_snapshot,
                history=history,
                remaining_steps=remaining,
                memory=memory_context,
            )

            decision_status = str(decision.get("status", "")).strip().lower()
            if decision_status == "complete":
                LOGGER.info(
                    "Run %s step %d: agentic fallback — brain says complete, no recovery",
                    run.run_id, failed_step.index + 1,
                )
                return False

            raw_action = decision.get("action")
            if not raw_action or not isinstance(raw_action, dict):
                LOGGER.warning(
                    "Run %s step %d: agentic fallback — brain returned invalid action: %r",
                    run.run_id, failed_step.index + 1, raw_action,
                )
                return False

            normalized_steps = normalize_plan_steps(
                [raw_action],
                max_steps=1,
                default_wait_ms=self._settings.planner_default_wait_ms,
            )
            if not normalized_steps:
                LOGGER.warning(
                    "Run %s step %d: agentic fallback — action normalization returned empty",
                    run.run_id, failed_step.index + 1,
                )
                return False

            step_input = {**normalized_steps[0], "_is_fallback": True}
            fallback_step = StepRuntimeState(
                index=len(run.steps),
                type=str(step_input.get("type", "step")),
                input=step_input,
                status=StepStatus.pending,
            )
            run.steps.append(fallback_step)
            self._run_store.persist(run)
            LOGGER.info(
                "Run %s step %d: agentic fallback injected step index=%d type=%s",
                run.run_id, failed_step.index + 1, fallback_step.index, fallback_step.type,
            )
            return True

        except Exception as exc:
            LOGGER.warning(
                "Run %s step %d: agentic fallback raised exception (%s) — skipping",
                run.run_id, failed_step.index + 1, self._compact_error(exc),
            )
            return False

    @staticmethod
    def _history_from_run(run: RunState) -> list[dict[str, Any]]:
        history: list[dict[str, Any]] = []
        for step in run.steps[-20:]:
            history.append(
                {
                    "step_index": step.index,
                    "type": step.type,
                    "input": step.input,
                    "status": step.status.value,
                    "message": step.message,
                    "error": step.error,
                }
            )
        return history

    def _build_autonomous_memory(self, run: RunState) -> dict[str, Any]:
        run_domain = self._extract_run_domain(run)
        completed_steps = [
            self._step_memory_summary(step)
            for step in run.steps
            if step.status == StepStatus.completed
        ]
        previous_successes: list[dict[str, Any]] = []
        for previous in self._run_store.list():
            if previous.run_id == run.run_id:
                continue
            if previous.status != RunStatus.completed:
                continue
            if run_domain and self._extract_run_domain(previous) != run_domain:
                continue
            previous_summary = {
                "run_id": previous.run_id,
                "run_name": previous.run_name,
                "prompt": (previous.prompt or "")[:300],
                "summary": (previous.summary or "")[:300],
                "steps": [
                    item
                    for item in (
                        self._step_memory_summary(step)
                        for step in previous.steps
                        if step.status == StepStatus.completed
                    )
                    if item
                ][:8],
            }
            if previous_summary["steps"] or previous_summary["summary"] or previous_summary["prompt"]:
                previous_successes.append(previous_summary)
            if len(previous_successes) >= 3:
                break

        memory: dict[str, Any] = {
            "domain": run_domain or "",
            "current_run_completed_steps": [item for item in completed_steps if item][-8:],
            "previous_successful_runs": previous_successes,
        }
        if run.selector_profile:
            memory["selector_profile_keys"] = sorted(run.selector_profile.keys())[:20]
        return memory

    @staticmethod
    def _step_memory_summary(step: StepRuntimeState) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "type": step.type,
        }
        raw_selector = step.input.get("selector")
        if isinstance(raw_selector, str) and raw_selector.strip():
            payload["selector"] = raw_selector.strip()
        if step.provided_selector:
            payload["resolved_selector"] = step.provided_selector.strip()
        if step.message:
            payload["message"] = step.message[:180]
        if step.type == "type":
            text_value = step.input.get("text")
            if isinstance(text_value, str) and text_value.strip():
                payload["text_hint"] = text_value.strip()[:80]
        if step.type == "select":
            value = step.input.get("value")
            if isinstance(value, str) and value.strip():
                payload["value"] = value.strip()[:80]
        if step.type == "drag":
            source_selector = step.input.get("source_selector")
            target_selector = step.input.get("target_selector")
            if isinstance(source_selector, str) and source_selector.strip():
                payload["source_selector"] = source_selector.strip()
            if isinstance(target_selector, str) and target_selector.strip():
                payload["target_selector"] = target_selector.strip()
        return payload if len(payload) > 1 else None

    async def _write_html_report(self, run: RunState) -> None:
        try:
            report_html = self._build_html_report(run)
            report_path = await self._files.write_text_artifact(run.run_id, "report.html", report_html)
            run.report_artifact = report_path
            self._run_store.persist(run)
        except Exception:
            LOGGER.exception("Failed to write HTML report for run %s", run.run_id)

    @staticmethod
    def _trace_json(payload: object) -> str:
        return json.dumps(payload, indent=2, ensure_ascii=False, default=str)

    def _active_step_trace(self) -> dict[str, Any] | None:
        return self._step_trace_context.get()

    def _active_step_state(self) -> StepRuntimeState | None:
        return self._step_state_context.get()

    def _record_step_trace_group(self, group: dict[str, Any]) -> dict[str, Any]:
        trace = self._active_step_trace()
        if trace is None:
            return group
        trace.setdefault("attempt_groups", []).append(group)
        return group

    @staticmethod
    def _record_group_attempt(group: dict[str, Any], attempt: dict[str, Any]) -> None:
        group.setdefault("attempts", []).append(attempt)

    @staticmethod
    def _elapsed_ms(started_at: float) -> float:
        return round((perf_counter() - started_at) * 1000, 2)

    def _fast_path_enabled(self) -> bool:
        return bool(getattr(self._settings, "execution_fast_path_enabled", True))

    def _fast_path_action_timeout_seconds(self) -> float:
        configured = float(getattr(self._settings, "execution_fast_path_action_timeout_seconds", 4))
        return max(1.0, configured)

    def _fast_path_selector_timeout_ms(self) -> int:
        configured = int(getattr(self._settings, "execution_fast_path_selector_timeout_ms", 2000))
        return max(500, configured)

    @staticmethod
    def _is_fast_selector_shape(selector: str) -> bool:
        text = selector.strip().lower()
        if not text:
            return False
        if "{{" in text or text.startswith("$") or text.startswith("profile:"):
            return False
        if any(token in text for token in ("iframe", "xpath=", "nth-of-type", ">> nth=", "drag", "dropzone")):
            return False
        return text.startswith(("#", ".", "[", "input", "button", "select", "textarea", "text="))

    def _classify_execution_path(self, run: RunState, step: StepRuntimeState) -> dict[str, Any]:
        if not self._fast_path_enabled():
            return {"path": "slow", "reason": "fast_path_disabled"}

        step_input = step.input or {}
        step_type = step.type

        if step_type == "navigate":
            return {"path": "fast", "reason": "simple_navigation"}

        if step_type == "wait":
            until = str(step_input.get("until", "timeout"))
            if until == "timeout":
                return {"path": "fast", "reason": "simple_timeout_wait"}
            return {"path": "slow", "reason": f"wait_condition_{until}"}

        if step_type not in {"click", "type", "select"}:
            return {"path": "slow", "reason": f"{step_type}_requires_full_pipeline"}

        grounded_selector = step_input.get("_grounded_selector")
        if isinstance(grounded_selector, str) and grounded_selector.strip():
            grounded_selector = grounded_selector.strip()
            if self._is_fast_selector_shape(grounded_selector):
                return {
                    "path": "fast",
                    "reason": "grounded_selector",
                    "candidate_count": 1,
                    "primary_selector": grounded_selector,
                }
            return {
                "path": "slow",
                "reason": "complex_grounded_selector",
                "primary_selector": grounded_selector,
            }

        raw_selector = step_input.get("selector")
        if not isinstance(raw_selector, str) or not raw_selector.strip():
            return {"path": "slow", "reason": "missing_selector"}
        if self._selector_alias_key(raw_selector):
            return {"path": "slow", "reason": "selector_alias"}

        selector_profile = run.selector_profile or {}
        test_data = run.test_data or {}
        run_domain = self._extract_run_domain(run)
        text_hint = self._step_text_hint(step_input)
        candidates = self._selector_candidates(
            raw_selector,
            step_type,
            selector_profile,
            test_data,
            run_domain,
            text_hint,
        )
        if not candidates:
            return {"path": "slow", "reason": "no_candidates"}
        primary_selector = candidates[0]
        if not self._is_fast_selector_shape(primary_selector):
            return {"path": "slow", "reason": "complex_primary_selector", "primary_selector": primary_selector}
        if len(candidates) > 3:
            return {"path": "slow", "reason": "too_many_candidates", "candidate_count": len(candidates)}

        return {
            "path": "fast",
            "reason": "simple_selector",
            "candidate_count": len(candidates),
            "primary_selector": primary_selector,
        }

    async def _dispatch_fast_step(self, run: RunState, raw_step: dict[str, Any], classification: dict[str, Any]) -> str:
        step_type = str(raw_step.get("type") or "")
        test_data = run.test_data or {}
        selector_profile = run.selector_profile or {}
        run_domain = self._extract_run_domain(run)
        intent = self._build_step_intent(step_type, str(raw_step.get("selector") or ""), self._step_text_hint(raw_step))

        if step_type == "navigate":
            target_url = self._apply_template(str(raw_step["url"]), test_data, run_id=run.run_id)
            return await asyncio.wait_for(
                self._browser.navigate(target_url),
                timeout=self._fast_path_action_timeout_seconds(),
            )

        if step_type == "wait":
            return await asyncio.wait_for(
                self._browser.wait_for(
                    until=str(raw_step.get("until", "timeout")),
                    ms=raw_step.get("ms"),
                    selector=str(raw_step.get("selector")) if raw_step.get("selector") else None,
                    load_state=str(raw_step.get("load_state")) if raw_step.get("load_state") else None,
                ),
                timeout=self._fast_path_action_timeout_seconds(),
            )

        selector = str(raw_step["selector"])
        text_hint = self._step_text_hint(raw_step)
        # Prefer perception-grounded selector (identified from live DOM) over
        # the classification's profile-generated primary_selector.
        grounded = raw_step.get("_grounded_selector")
        resolved_selector = (
            grounded
            or classification.get("primary_selector")
            or await self._resolve_selector(
                selector,
                step_type,
                selector_profile,
                test_data,
                run_domain,
                text_hint,
            )
        )
        if grounded:
            LOGGER.debug("Fast path: using perception-grounded selector=%r for step_type=%s", grounded, step_type)
        active_step = self._active_step_state()
        if active_step is not None:
            active_step.provided_selector = resolved_selector

        trace_group = self._record_step_trace_group(
            {
                "kind": "fast_path",
                "step_type": step_type,
                "raw_selector": selector,
                "intent": self._serialize_step_intent(intent),
                "resolved_selector": resolved_selector,
                "final_selected_selector": resolved_selector,
                "candidate_count": classification.get("candidate_count", 1),
                "attempts": [],
            }
        )

        attempt_started = perf_counter()
        post_validate_ms = 0.0
        try:
            if step_type == "click":
                before_snapshot = await self._safe_page_snapshot()
                result = await asyncio.wait_for(
                    self._browser.click(resolved_selector),
                    timeout=self._fast_path_action_timeout_seconds(),
                )
                validate_started = perf_counter()
                validation = await self._validate_click_post_action(
                    resolved_selector=resolved_selector,
                    before_snapshot=before_snapshot,
                    raw_selector=selector,
                    text_hint=text_hint,
                )
                post_validate_ms = self._elapsed_ms(validate_started)
                if validation:
                    result = f"{result}; {validation}"
            elif step_type == "type":
                raw_text = str(raw_step["text"])
                text = self._apply_template(raw_text, test_data, run_id=run.run_id)
                clear_first = bool(raw_step.get("clear_first", True))
                if "{{random" in raw_text.lower() or "random" in raw_text.lower():
                    selector_field_name = self._run_context_selector_field_name(selector)
                    run_context = self._run_context.setdefault(run.run_id, {})
                    run_context["last_typed_value"] = text
                    if selector_field_name:
                        run_context[f"last_typed_{selector_field_name}"] = text
                result = await asyncio.wait_for(
                    self._browser.type_text(
                        selector=resolved_selector,
                        text=text,
                        clear_first=clear_first,
                    ),
                    timeout=self._fast_path_action_timeout_seconds(),
                )
                validate_started = perf_counter()
                validation = await self._validate_type_post_action(
                    resolved_selector=resolved_selector,
                    expected_text=text,
                    clear_first=clear_first,
                )
                post_validate_ms = self._elapsed_ms(validate_started)
                if validation:
                    result = f"{result}; {validation}"
            elif step_type == "select":
                value = self._apply_template(str(raw_step["value"]), test_data, run_id=run.run_id)
                result = await asyncio.wait_for(
                    self._browser.select(selector=resolved_selector, value=value),
                    timeout=self._fast_path_action_timeout_seconds(),
                )
                validate_started = perf_counter()
                validation = await self._validate_select_post_action(
                    resolved_selector=resolved_selector,
                    expected_value=value,
                )
                post_validate_ms = self._elapsed_ms(validate_started)
                if validation:
                    result = f"{result}; {validation}"
            else:
                raise _FastPathEscalation(f"Unsupported fast-path step type: {step_type}")

            self._remember_selector_success(
                run_domain=run_domain,
                step_type=step_type,
                raw_selector=selector,
                resolved_selector=resolved_selector,
                text_hint=text_hint,
            )
            # Store the element signature so future runs can use signature-based
            # recovery even when the fast path is taken (perception may have
            # grounded the element; if not, _remember_selector_signature returns
            # early and the post-success extraction in _execute_step fills the gap).
            self._remember_selector_signature(
                run_domain=run_domain,
                step_type=step_type,
                raw_selector=selector,
                text_hint=text_hint,
            )
            trace_group["result"] = result
            self._record_group_attempt(
                trace_group,
                {
                    "phase": "fast_path",
                    "selector": resolved_selector,
                    "status": "success",
                    "elapsed_ms": self._elapsed_ms(attempt_started),
                    "post_validate_ms": post_validate_ms,
                    "total_attempt_ms": self._elapsed_ms(attempt_started),
                    "result": result,
                },
            )
            return result
        except Exception as exc:
            compact = self._compact_error(exc)
            # "Execution context was destroyed" means the click triggered a page
            # navigation — the action SUCCEEDED.  Don't escalate; return success.
            if "execution context was destroyed" in compact.lower():
                result = f"Clicked {resolved_selector} (navigation detected)"
                self._remember_selector_success(
                    run_domain=run_domain,
                    step_type=step_type,
                    raw_selector=selector,
                    resolved_selector=resolved_selector,
                    text_hint=text_hint,
                )
                self._remember_selector_signature(
                    run_domain=run_domain,
                    step_type=step_type,
                    raw_selector=selector,
                    text_hint=text_hint,
                )
                trace_group["result"] = result
                self._record_group_attempt(
                    trace_group,
                    {
                        "phase": "fast_path",
                        "selector": resolved_selector,
                        "status": "success_navigation",
                        "elapsed_ms": self._elapsed_ms(attempt_started),
                        "total_attempt_ms": self._elapsed_ms(attempt_started),
                        "result": result,
                    },
                )
                return result
            trace_group["final_error"] = compact
            self._record_group_attempt(
                trace_group,
                {
                    "phase": "fast_path",
                    "selector": resolved_selector,
                    "status": "failed",
                    "elapsed_ms": self._elapsed_ms(attempt_started),
                    "post_validate_ms": post_validate_ms,
                    "total_attempt_ms": self._elapsed_ms(attempt_started),
                    "error": compact,
                },
            )
            raise _FastPathEscalation(compact) from exc

    async def _perceive_before_act(
        self,
        run: RunState,
        step: StepRuntimeState,
        snapshot: dict[str, Any] | None = None,
    ) -> PerceptionMatch | None:
        """
        Identify the live DOM element that best matches this step's intent
        BEFORE attempting any selector.

        Pass *snapshot* to reuse an already-fetched inspect_page() result
        (zero extra cost).  If omitted, a fresh inspect_page() call is made.

        Returns a PerceptionMatch on confident identification.
        Returns None when the page is empty, intent is too vague, or no
        element scores high enough — the existing pipeline handles those.
        """
        step_input = step.input or {}
        raw_selector = str(step_input.get("selector", "")).strip() or self._selector_seed_from_target(step_input, step.type)
        text_hint = self._step_text_hint(step_input)

        # Extract clean visible text from selector syntax (text=..., :has-text(...),
        # :text(...), :text-is(...)) so the tokenizer receives the exact visible label
        # rather than polluted CSS syntax.
        if not text_hint and raw_selector:
            text_hint = self._extract_selector_text(raw_selector)

        intent_text = " ".join(p for p in [text_hint or raw_selector] if p).strip()
        if intent_text:
            LOGGER.debug(
                "Perception: intent_text=%r (text_hint=%r raw_selector=%r) step_type=%s",
                intent_text[:80], (text_hint or "")[:80], (raw_selector or "")[:80], step.type,
            )
        if not intent_text:
            return None

        if snapshot is None:
            try:
                snapshot = await asyncio.wait_for(
                    self._browser.inspect_page(include_screenshot=False),
                    timeout=4.0,
                )
            except Exception as exc:
                LOGGER.debug("Perception: inspect_page failed (step %d): %s", step.index + 1, exc)
                return None

        element_index = build_element_index(snapshot)
        if element_index.count == 0:
            LOGGER.debug("Perception: no interactive elements at step %d", step.index + 1)
            return None

        return find_best_match(
            intent_text=intent_text,
            step_type=step.type,
            element_index=element_index,
        )

    @staticmethod
    def _validate_medium_grounding(
        match: PerceptionMatch,
        step_type: str,
        step_intent: "StepIntent | None",
    ) -> tuple[bool, str]:
        """
        Validate a medium-confidence perception match before trusting it as a
        grounded selector hint.

        Checks:
          1. Element is enabled (visibility is guaranteed by build_element_index).
          2. Element tag/role is plausible for the intended action type.
          3. At least one meaningful word from step_intent.target_text appears in the
             element's visible text, aria label, placeholder, or name.

        Returns (passed: bool, reason: str).
        """
        el = match.element

        # 1. Enabled check
        if not el.enabled:
            return False, "element_disabled"

        # 2. Role/tag plausibility per action type
        if step_type == "click":
            plausible = (
                el.tag in {"button", "a", "input", "label"}
                or el.role in {"button", "link", "menuitem", "tab", "checkbox", "radio", "option", "switch"}
                or el.el_type in {"submit", "button", "checkbox", "radio"}
            )
            if not plausible:
                return False, f"not_clickable tag={el.tag!r} role={el.role!r}"
        elif step_type == "type":
            if el.tag == "button" or el.role == "button":
                return False, "is_button_not_typeable"
            if not (
                el.tag in {"input", "textarea"}
                or el.role in {"textbox", "searchbox", "combobox"}
            ):
                return False, f"not_typeable tag={el.tag!r} role={el.role!r}"
        elif step_type == "select":
            if not (el.tag == "select" or el.role in {"combobox", "listbox"}):
                return False, f"not_selectable tag={el.tag!r} role={el.role!r}"

        # 3. Text/label alignment: at least one word from target_text must appear
        #    in the element's searchable text surface.
        if step_intent and step_intent.target_text:
            target_words = {
                w for w in re.findall(r"[a-z0-9]+", step_intent.target_text.lower())
                if len(w) >= 3
            }
            if target_words:
                haystack = " ".join([
                    el.text, el.aria, el.placeholder, el.name, el.title,
                ]).lower()
                if not any(w in haystack for w in target_words):
                    return False, (
                        f"no_label_overlap target={step_intent.target_text!r} "
                        f"element_text={el.text[:40]!r}"
                    )

        return True, "ok"

    async def _execute_step(self, run: RunState, step: StepRuntimeState) -> None:
        step_started_perf = perf_counter()
        step.status = StepStatus.running
        step.started_at = utc_now()
        step.error = None
        step.message = None
        step.failure_screenshot = None
        step.user_input_kind = None
        step.user_input_prompt = None
        step.requested_selector_target = None
        step_trace: dict[str, Any] = {
            "run_id": run.run_id,
            "step_id": step.step_id,
            "index": step.index,
            "type": step.type,
            "action": step.type,
            "input": dict(step.input),
            "status": "running",
            "started_at": step.started_at,
            "attempt_groups": [],
        }
        selector_probe = None
        for field in ("selector", "source_selector", "target_selector"):
            value = step.input.get(field)
            if isinstance(value, str) and value.strip():
                selector_probe = value.strip()
                break
        if selector_probe is None:
            selector_probe = self._selector_seed_from_target(step.input, step.type) or None
        step_intent = self._build_step_intent(step.type, selector_probe, self._step_text_hint(step.input))
        step_trace["intent"] = self._serialize_step_intent(step_intent)
        trace_token = self._step_trace_context.set(step_trace)
        step_token = self._step_state_context.set(step)

        try:
            before_snapshot = await self._safe_page_snapshot()
            step_trace["page_state_before"] = self._summarize_page_state(before_snapshot)

            # ------------------------------------------------------------------
            # PRE-ACTION PAGE HEALTH CHECK
            # Detect error pages, domain mismatches, blocking modals, and
            # loading states BEFORE spending any time on selector attempts.
            # ------------------------------------------------------------------
            page_health = self._check_page_health(before_snapshot, run, step)
            step_trace["page_health"] = page_health
            if page_health["status"] == "block":
                for issue in page_health.get("issues", []):
                    LOGGER.error(
                        "Run %s step %d (type=%s): pre-action health check BLOCKED [%s] %s",
                        run.run_id, step.index + 1, step.type,
                        issue["type"], issue["detail"],
                    )
                first_issue = (page_health.get("issues") or [{}])[0]
                raise ValueError(
                    f"Pre-action page health check failed ({first_issue.get('type', 'unknown')}): "
                    f"{first_issue.get('detail', 'see logs')}"
                )
            elif page_health["status"] == "warn":
                for issue in page_health.get("issues", []):
                    LOGGER.warning(
                        "Run %s step %d (type=%s): pre-action health check WARN [%s] %s",
                        run.run_id, step.index + 1, step.type,
                        issue["type"], issue["detail"],
                    )

            # ------------------------------------------------------------------
            # PERCEIVE FIRST — reuse the already-fetched page snapshot (zero
            # extra network/browser cost) to identify the target element from
            # what is actually visible on the page right now.
            # ------------------------------------------------------------------
            strong_live_grounding = False
            if step.type in {"click", "type", "select"} and before_snapshot:
                perception_match = await self._perceive_before_act(
                    run, step, snapshot=before_snapshot
                )
                if perception_match is not None:
                    step_trace["perception"] = {
                        "confidence": perception_match.confidence,
                        "score": perception_match.score,
                        "grounded_selector": perception_match.selector,
                        "alternative_count": perception_match.alternative_count,
                        "element_text": perception_match.element.text[:80],
                        "element_tag": perception_match.element.tag,
                        "element_role": perception_match.element.role,
                    }
                    if perception_match.confidence in {"unique", "high"}:
                        step.input = {
                            **step.input,
                            "_grounded_selector": perception_match.selector,
                            "_grounded_signature": perception_match.element.signature(),
                        }
                        strong_live_grounding = True
                        LOGGER.info(
                            "Run %s step %d (type=%s): perception GROUNDED selector=%r "
                            "(confidence=%s score=%d alternatives=%d element=%r)",
                            run.run_id, step.index + 1, step.type,
                            perception_match.selector, perception_match.confidence,
                            perception_match.score, perception_match.alternative_count,
                            perception_match.element.text[:50],
                        )
                    elif perception_match.confidence == "medium":
                        # Medium confidence: validate the candidate before trusting it.
                        # An invalid medium match must not pollute the fallback pipeline.
                        med_valid, med_reason = self._validate_medium_grounding(
                            perception_match, step.type, step_intent
                        )
                        step_trace["perception"]["validation_passed"] = med_valid
                        step_trace["perception"]["validation_reason"] = med_reason
                        if med_valid:
                            step.input = {
                                **step.input,
                                "_grounded_selector": perception_match.selector,
                                "_grounded_signature": perception_match.element.signature(),
                            }
                            LOGGER.info(
                                "Run %s step %d (type=%s): perception MEDIUM VALIDATED selector=%r "
                                "(score=%d alternatives=%d element=%r) — used as grounded hint",
                                run.run_id, step.index + 1, step.type,
                                perception_match.selector, perception_match.score,
                                perception_match.alternative_count,
                                perception_match.element.text[:50],
                            )
                        else:
                            LOGGER.warning(
                                "Run %s step %d (type=%s): perception MEDIUM REJECTED selector=%r "
                                "(score=%d reason=%r element=%r) — falling back to selector pipeline",
                                run.run_id, step.index + 1, step.type,
                                perception_match.selector, perception_match.score,
                                med_reason, perception_match.element.text[:50],
                            )
                    elif perception_match.confidence == "ambiguous":
                        # Ambiguous: multiple elements scored similarly — we cannot
                        # reliably identify the intended target.  Do NOT set
                        # _grounded_selector: injecting an unreliable selector would
                        # either trigger the fast path on a wrong element or bias the
                        # slow pipeline toward the wrong candidate.
                        # Let the full selector pipeline run clean.
                        step_trace["perception"]["grounding_action"] = "suppressed"
                        step_trace["perception"]["grounding_reason"] = (
                            f"ambiguous — {perception_match.alternative_count} alternative(s) "
                            f"scored similarly (top_score={perception_match.score})"
                        )
                        LOGGER.warning(
                            "Run %s step %d (type=%s): perception AMBIGUOUS SUPPRESSED "
                            "(top=%r score=%d alternatives=%d) — full selector pipeline used",
                            run.run_id, step.index + 1, step.type,
                            perception_match.selector, perception_match.score,
                            perception_match.alternative_count,
                        )

            if strong_live_grounding:
                step_trace["page_state_assertion"] = {
                    "status": "passed",
                    "reason": "strong_live_grounding",
                    "grounded_selector": step.input.get("_grounded_selector"),
                }
            else:
                try:
                    page_assertion = await self._assert_step_page_state(run, step, before_snapshot, step_intent)
                except ValueError as page_assert_exc:
                    if step.type in {"click", "type", "select", "wait", "handle_popup"}:
                        step_trace["page_state_assertion"] = {
                            "status": "warn",
                            "reason": "assertion_failed_but_pipeline_continues",
                            "detail": str(page_assert_exc),
                        }
                        LOGGER.warning(
                            "Run %s step %d (type=%s): page assertion failed (%s) but selector pipeline will continue",
                            run.run_id,
                            step.index + 1,
                            step.type,
                            self._compact_error(page_assert_exc),
                        )
                    else:
                        raise
                else:
                    if page_assertion:
                        step_trace["page_state_assertion"] = page_assertion

            classification = self._classify_execution_path(run, step)
            step_trace["execution_path"] = classification.get("path")
            step_trace["execution_path_reason"] = classification.get("reason")
            step_trace["execution_path_metadata"] = {
                key: value
                for key, value in classification.items()
                if key not in {"path", "reason"}
            }

            slow_path_pre_state: dict[str, Any] | None = None
            if classification.get("path") == "fast":
                LOGGER.debug(
                    "Run %s step %d type=%s: taking FAST path (reason=%s selector=%r)",
                    run.run_id, step.index + 1, step.type,
                    classification.get("reason"), classification.get("primary_selector", ""),
                )
                try:
                    message = await self._dispatch_fast_step(run, step.input, classification)
                    step_trace["execution_path"] = "fast"
                except _FastPathEscalation as fast_exc:
                    fast_error = self._compact_error(fast_exc)
                    step_trace["fast_path_error"] = fast_error
                    LOGGER.warning(
                        "Run %s step %d type=%s: fast path failed (%s), escalating to slow path. selector=%r",
                        run.run_id, step.index + 1, step.type,
                        fast_error, step.input.get("selector", ""),
                    )
                    if step.type != "handle_popup":
                        await self._auto_handle_known_popups(run)
                    message = await asyncio.wait_for(
                        self._dispatch_step(run, step.input),
                        timeout=self._settings.step_timeout_seconds,
                    )
                    step_trace["execution_path"] = "fast->slow"
            else:
                LOGGER.debug(
                    "Run %s step %d type=%s: taking SLOW path (reason=%s selector=%r)",
                    run.run_id, step.index + 1, step.type,
                    classification.get("reason"), step.input.get("selector", ""),
                )
                if step.type != "handle_popup":
                    await self._auto_handle_known_popups(run)
                # Capture pre-click element context (incl. parent_select_value for
                # option elements) before the dispatch mutates the page.  Only done
                # for click steps that already have a provided_selector so the call
                # is narrow and the result is None-safe if the element isn't found.
                if step.type == "click" and step.provided_selector:
                    slow_path_pre_state = await self._capture_click_pre_state(
                        step.provided_selector
                    )
                message = await asyncio.wait_for(
                    self._dispatch_step(run, step.input),
                    timeout=self._settings.step_timeout_seconds,
                )
                step_trace["execution_path"] = "slow"
            outcome_verification = await self._verify_step_outcome_against_page_state(
                run,
                step,
                message,
                before_snapshot=before_snapshot,
                click_pre_state=slow_path_pre_state,
            )
            step_trace["page_state_verification"] = outcome_verification
            step_trace["page_state_after"] = outcome_verification.get("after_state")
            step.status = StepStatus.completed
            step.message = message
            step_trace["result"] = message

            # ------------------------------------------------------------------
            # POST-SUCCESS SIGNATURE CAPTURE
            # If perception did not run (or was ambiguous) for this step, the
            # element signature was never stored.  We extract it now from the
            # pre-action page snapshot using the winning selector so that future
            # runs can use signature-based recovery instead of immediately falling
            # through to LLM recovery.
            # ------------------------------------------------------------------
            if (
                step.type in {"click", "type", "select"}
                and before_snapshot
                and step.provided_selector
                and not step.input.get("_grounded_signature")
            ):
                extracted_sig = self._extract_signature_from_snapshot(
                    step.provided_selector, before_snapshot
                )
                if extracted_sig:
                    raw_sel = str((step.input or {}).get("selector", step.provided_selector)).strip()
                    text_hint_val = self._step_text_hint(step.input or {})
                    run_domain_val = self._extract_run_domain(run)
                    self._remember_selector_signature(
                        run_domain=run_domain_val,
                        step_type=step.type,
                        raw_selector=raw_sel,
                        text_hint=text_hint_val,
                        signature=extracted_sig,
                    )
                    step_trace["snapshot_signature_captured"] = True
                    LOGGER.debug(
                        "Run %s step %d: captured element signature from snapshot "
                        "(selector=%r tag=%r text=%r)",
                        run.run_id, step.index + 1,
                        step.provided_selector,
                        extracted_sig.get("tag"), extracted_sig.get("text", "")[:40],
                    )

            original_selector_request = step.input.get("_selector_help_original")
            if (
                isinstance(original_selector_request, str)
                and original_selector_request.strip()
                and step.provided_selector
            ):
                self._remember_selector_success(
                    run_domain=self._extract_run_domain(run),
                    step_type=step.type,
                    raw_selector=original_selector_request.strip(),
                    resolved_selector=step.provided_selector,
                    text_hint=None,
                )
        except Exception as exc:
            compact = self._compact_error(exc)
            step_trace["exception"] = compact
            # Build root-cause chain for logging
            root_cause = exc
            cause_chain: list[str] = []
            _walk = exc
            while _walk is not None:
                cause_chain.append(f"{type(_walk).__name__}: {_walk}")
                _walk = getattr(_walk, "__cause__", None) or getattr(_walk, "__context__", None)
                if _walk is exc:
                    break
            root_cause_str = " <- ".join(cause_chain)

            # Pause and request user selector input only when the element could NOT be
            # located on the page (selector is broken / element absent).
            # Hard-exclude post-action outcome failures — these mean the element WAS
            # found and actioned but the page reacted unexpectedly (form rejected,
            # navigation didn't happen). A different selector won't fix those.
            _INTERACTION_STEP_TYPES = {"click", "type", "select", "drag", "handle_popup", "verify_text"}
            _error_lower = compact.lower()
            # Hard excludes take priority — even if a "not found" phrase also appears
            _OUTCOME_FAILURE_PHRASES = (
                "click effect not observed",
                "page url/title/text stayed the same",
                "element remained visible/enabled",
                "post_validation=failed",
            )
            _is_outcome_failure = any(p in _error_lower for p in _OUTCOME_FAILURE_PHRASES)
            _LOCATOR_FAILURE_PHRASES = (
                "waiting for locator",
                "locator.click: timeout",
                "locator.fill: timeout",
                "locator.select_option: timeout",
                "element not found",
                "no element",
                "strict mode violation",
                "grounded selection failed",
                "all selector candidates failed",
                "does not contain a matching live element",
            )
            # "all selector candidates failed" paired with an outcome failure means the
            # click happened but the outcome check failed — not a locator problem.
            _is_element_not_found = (
                not _is_outcome_failure
                and any(p in _error_lower for p in _LOCATOR_FAILURE_PHRASES)
            )
            # verify_text failures mean the selector pointed at the wrong element —
            # treat as a selector problem so the user can correct it.
            if step.type == "verify_text" and "text verification failed" in _error_lower:
                _is_element_not_found = True
            _has_selector_field = any(
                isinstance((step.input or {}).get(f), str)
                and bool((step.input or {}).get(f, "").strip())
                for f in ("selector", "source_selector", "target_selector")
            )
            if (
                step.type in _INTERACTION_STEP_TYPES
                and _has_selector_field
                and _is_element_not_found
            ):
                LOGGER.warning(
                    "Run %s step %d/%d (type=%s): selector help requested. selector=%r root_cause=%s",
                    run.run_id, step.index + 1, len(run.steps), step.type,
                    step.input.get("selector", ""), root_cause_str,
                )
                step.provided_selector = None  # clear so each retry is treated as fresh
                step.status = StepStatus.waiting_for_input
                step.error = compact
                step.message = "Waiting for selector help"
                step.user_input_kind = "selector"
                step.requested_selector_target = self._requested_selector_target(step)
                step.user_input_prompt = self._build_selector_help_prompt(step)
                step_trace["result"] = step.message
                # Capture screenshot + diagnosis so the UI shows "What went wrong",
                # "Suggested fix", and "Try these selectors" even while paused.
                await self._capture_failure_screenshot(run.run_id, step)
                await self._run_failure_diagnosis(run, step)
            else:
                LOGGER.error(
                    "Run %s step %d/%d (type=%s) FAILED. selector=%r provided_selector=%r "
                    "error=%s root_cause=%s",
                    run.run_id, step.index + 1, len(run.steps), step.type,
                    step.input.get("selector", ""), step.provided_selector,
                    compact, root_cause_str,
                )
                step.status = StepStatus.failed
                if isinstance(exc, TimeoutError):
                    step.error = f"{compact} (step_type={step.type})"
                else:
                    step.error = compact
                step.message = "Step failed"
                step_trace["result"] = step.message
                await self._capture_failure_screenshot(run.run_id, step)
                await self._run_failure_diagnosis(run, step)
        finally:
            step.ended_at = utc_now()
            step_trace["status"] = step.status.value
            step_trace["message"] = step.message
            step_trace["error"] = step.error
            step_trace["provided_selector"] = step.provided_selector
            step_trace["requested_selector_target"] = step.requested_selector_target
            step_trace["failure_screenshot"] = step.failure_screenshot
            step_trace["ended_at"] = step.ended_at
            step_trace["total_step_ms"] = self._elapsed_ms(step_started_perf)
            log_filename = f"step-{step.index:03d}.log"
            trace_filename = f"step-{step.index:03d}.trace.json"
            trace_error_filename = f"step-{step.index:03d}.trace-error.log"
            log_lines = [
                f"type={step.type}",
                f"status={step.status.value}",
            ]
            if step.message:
                log_lines.append(f"message={step.message}")
            if step.error:
                log_lines.append(f"error={step.error}")
            if step.provided_selector:
                log_lines.append(f"provided_selector={step.provided_selector}")
            try:
                await self._files.write_text_artifact(
                    run.run_id,
                    log_filename,
                    "\n".join(log_lines),
                )
            except Exception:
                LOGGER.exception("Failed to write step log for run %s step %s", run.run_id, step.index)
            try:
                trace_payload = self._trace_json(step_trace)
            except Exception as trace_json_exc:
                compact = self._compact_error(trace_json_exc)
                LOGGER.exception(
                    "Failed to serialize step trace for run %s step %s",
                    run.run_id,
                    step.index,
                )
                try:
                    await self._files.write_text_artifact(
                        run.run_id,
                        trace_error_filename,
                        f"Trace serialization failed: {compact}",
                    )
                except Exception:
                    LOGGER.exception(
                        "Failed to write step trace serialization error for run %s step %s",
                        run.run_id,
                        step.index,
                    )
            else:
                try:
                    await self._files.write_text_artifact(
                        run.run_id,
                        trace_filename,
                        trace_payload,
                    )
                except Exception as trace_write_exc:
                    compact = self._compact_error(trace_write_exc)
                    LOGGER.exception("Failed to write step trace for run %s step %s", run.run_id, step.index)
                    try:
                        await self._files.write_text_artifact(
                            run.run_id,
                            trace_error_filename,
                            f"Trace write failed: {compact}\ntrace_file={trace_filename}\ntrace_size={len(trace_payload)}",
                        )
                    except Exception:
                        LOGGER.exception(
                            "Failed to write step trace error artifact for run %s step %s",
                            run.run_id,
                            step.index,
                        )
            self._step_trace_context.reset(trace_token)
            self._step_state_context.reset(step_token)

    def _should_continue_after_failure(self, run: RunState) -> bool:
        return getattr(run, "failure_mode", "stop") == "continue"

    def _mark_remaining_steps_skipped(self, run: RunState, start_index: int) -> None:
        for step in run.steps[start_index:]:
            if step.status != StepStatus.pending:
                continue
            step.status = StepStatus.skipped
            step.started_at = step.started_at or utc_now()
            step.ended_at = utc_now()
            step.message = "Skipped because an earlier step failed."
            step.error = None

    async def _try_llm_selector_recovery(
        self,
        run: RunState | Any,
        step: StepRuntimeState,
        error: Exception,
        *,
        run_domain: str | None = None,
    ) -> str | None:
        recovery_started = perf_counter()
        if not self._selector_llm_recovery_enabled():
            return None
        if not self._llm_recovery_allowed_for_active_step():
            return None
        if not self._should_request_selector_help(step, error):
            return None

        selector_field = self._editable_selector_field(step)
        if not selector_field:
            return None
        raw_selector = step.input.get(selector_field)
        if not isinstance(raw_selector, str) or not raw_selector.strip():
            return None
        text_hint = self._step_text_hint(step.input)
        intent = self._build_step_intent(step.type, raw_selector.strip(), text_hint)
        effective_run_domain = run_domain
        if effective_run_domain is None and isinstance(run, RunState):
            effective_run_domain = self._extract_run_domain(run)

        snapshot_ms = 0.0
        suggestions_ms = 0.0
        trace_group = self._record_step_trace_group(
            {
                "kind": "llm_selector_recovery",
                "step_type": step.type,
                "failed_selector": raw_selector.strip() if isinstance(raw_selector, str) else None,
                "intent": self._serialize_step_intent(intent),
                "error_message": self._compact_error(error),
                "attempts": [],
            }
        )

        try:
            snapshot_started = perf_counter()
            page_snapshot = await self._browser.inspect_page(include_screenshot=False)
            snapshot_ms = self._elapsed_ms(snapshot_started)
        except Exception:
            return None

        # Pull element signature from perception grounding (may be None if
        # perception did not run or found no confident match for this step).
        element_hint: dict | None = step.input.get("_grounded_signature") or None
        if element_hint:
            LOGGER.debug(
                "LLM recovery: passing element_hint to brain (tag=%r text=%r aria=%r testid=%r)",
                element_hint.get("tag"), element_hint.get("text"),
                element_hint.get("aria"), element_hint.get("testid"),
            )

        try:
            suggestions_started = perf_counter()
            suggestions = await self._brain.suggest_selectors(
                step_type=step.type,
                failed_selector=raw_selector.strip(),
                error_message=self._compact_error(error),
                page=page_snapshot,
                text_hint=text_hint,
                max_candidates=self._selector_llm_max_candidates(),
                element_hint=element_hint,
            )
            suggestions_ms = self._elapsed_ms(suggestions_started)
        except Exception:
            suggestions = []

        suggestions = self._filter_llm_selector_suggestions(
            step_type=step.type,
            failed_selector=raw_selector.strip(),
            suggestions=suggestions,
            intent=intent,
        )
        trace_group["page_snapshot_ms"] = snapshot_ms
        trace_group["selector_suggestions_ms"] = suggestions_ms
        trace_group["filtered_suggestions"] = list(suggestions)

        for suggestion in suggestions:
            candidate = suggestion.strip()
            if not candidate:
                continue
            original_input = dict(step.input)
            original_provided_selector = step.provided_selector
            dispatch_started = perf_counter()
            try:
                step.input[selector_field] = candidate
                step.provided_selector = candidate
                message = await asyncio.wait_for(
                    self._dispatch_step(run, step.input),
                    timeout=max(float(self._settings.step_timeout_seconds) * 0.75, 1.0),
                )
                self._record_group_attempt(
                    trace_group,
                    {
                        "selector": candidate,
                        "status": "success",
                        "dispatch_ms": self._elapsed_ms(dispatch_started),
                        "elapsed_ms": self._elapsed_ms(dispatch_started),
                    },
                )
                self._remember_selector_success(
                    run_domain=effective_run_domain,
                    step_type=step.type,
                    raw_selector=raw_selector.strip(),
                    resolved_selector=candidate,
                    text_hint=text_hint,
                )
                trace_group["total_recovery_ms"] = self._elapsed_ms(recovery_started)
                return f"{message} (LLM selector recovery)"
            except Exception as exc:
                self._record_group_attempt(
                    trace_group,
                    {
                        "selector": candidate,
                        "status": "failed",
                        "dispatch_ms": self._elapsed_ms(dispatch_started),
                        "elapsed_ms": self._elapsed_ms(dispatch_started),
                        "error": self._compact_error(exc),
                    },
                )
                step.input = dict(original_input)
                step.provided_selector = original_provided_selector
                continue
        trace_group["total_recovery_ms"] = self._elapsed_ms(recovery_started)
        return None

    def _filter_llm_selector_suggestions(
        self,
        *,
        step_type: str,
        failed_selector: str,
        suggestions: list[str],
        intent: StepIntent | None = None,
    ) -> list[str]:
        if step_type != "click":
            cleaned = [item for item in suggestions if item.strip()]
            if intent:
                narrowed, _ = self._confidence_gate_candidates(cleaned, intent, step_type=step_type, source="live")
                return narrowed
            return cleaned

        failed_lower = failed_selector.strip().lower()
        amazon_position = self._amazon_result_position(failed_lower)
        if amazon_position is None or amazon_position <= 1:
            cleaned = [item for item in suggestions if item.strip()]
            if intent:
                narrowed, _ = self._confidence_gate_candidates(cleaned, intent, step_type=step_type, source="live")
                return narrowed
            return cleaned

        filtered: list[str] = []
        for suggestion in suggestions:
            candidate = suggestion.strip()
            if not candidate:
                continue
            candidate_lower = candidate.lower()
            if ".a-link-normal" in candidate_lower and "h2 a" not in candidate_lower and "title-recipe-title" not in candidate_lower:
                continue
            if "s-search-result" in candidate_lower or "title-recipe" in candidate_lower:
                candidate_position = self._amazon_result_position(candidate_lower)
                if candidate_position is not None and candidate_position != amazon_position:
                    continue
                if candidate_position is None and ">> nth=" not in candidate_lower:
                    continue
            filtered.append(candidate)
        if intent:
            narrowed, _ = self._confidence_gate_candidates(filtered, intent, step_type=step_type, source="live")
            return narrowed
        return filtered

    async def _auto_handle_known_popups(self, run: RunState) -> None:
        try:
            snapshot = await self._browser.inspect_page(include_screenshot=False)
        except Exception:
            return
        if not self._looks_like_popup_blocker(snapshot):
            return

        run_domain = self._extract_run_domain(run)
        popup_plans = (
            ("popup_accept", "accept"),
            ("popup_dismiss", "dismiss"),
        )
        for profile_key, policy in popup_plans:
            selectors = self._merge_profile_candidates(profile_key, run.selector_profile or {}, run_domain=run_domain)
            for selector in selectors:
                try:
                    result = await asyncio.wait_for(
                        self._browser.handle_popup(policy=policy, selector=selector),
                        timeout=2.5,
                    )
                except Exception:
                    continue
                result_lower = result.lower()
                if "handled" not in result_lower and "clicked" not in result_lower:
                    continue
                self._remember_selector_success(
                    run_domain=run_domain,
                    step_type="handle_popup",
                    raw_selector=profile_key,
                    resolved_selector=selector,
                    text_hint=policy,
                )
                return

    @staticmethod
    def _looks_like_popup_blocker(snapshot: dict[str, Any]) -> bool:
        text_excerpt = str(snapshot.get("text_excerpt", "")).lower()
        interactive_elements = snapshot.get("interactive_elements")

        # If the page contains success/confirmation signals, do NOT treat it as a
        # blocking popup — these are legitimate result pages the run needs to see.
        success_signals = (
            "successfully",
            "success",
            "confirmed",
            "thank you",
            "thanks",
            "registered",
            "account created",
            "order placed",
            "payment",
            "submitted",
            "sent",
            "welcome",
        )
        if any(token in text_excerpt for token in success_signals):
            return False

        popup_signals = (
            "cookie",
            "cookies",
            "consent",
            "privacy",
            "gdpr",
            "we use cookies",
            "accept all",
            "allow all",
            "alle akzeptieren",
            "akzeptieren",
            "zustimmen",
        )
        if any(token in text_excerpt for token in popup_signals):
            return True
        if isinstance(interactive_elements, list):
            for item in interactive_elements[:20]:
                if not isinstance(item, dict):
                    continue
                haystack = " ".join(
                    str(item.get(field, "")).lower()
                    for field in ("text", "aria", "name", "id", "testid", "role", "title")
                )
                if any(token in haystack for token in popup_signals):
                    return True
        return False

    def apply_manual_selector_hint(self, run_id: str, step_id: str, selector: str) -> RunState | None:
        run = self._run_store.get(run_id)
        if not run:
            return None

        step = next((item for item in run.steps if item.step_id == step_id), None)
        if not step:
            return None
        if run.status in {RunStatus.completed, RunStatus.cancelled}:
            raise ValueError("This run has already finished and cannot be resumed.")
        if run.status == RunStatus.failed and run.summary and "no selector was provided within" in run.summary:
            raise ValueError("Run finished: the selector input window has expired.")
        if not self._can_accept_manual_selector_hint(step):
            raise ValueError("This step is not eligible for selector input recovery.")
        # User provided a selector in time — cancel the auto-close countdown.
        self._cancel_selector_input_timeout(run.run_id)

        requested_selector = self._requested_selector_target(step)
        if not requested_selector:
            raise ValueError("This step does not support selector input recovery.")
        if step.type == "click":
            lowered = selector.strip().lower()
            if lowered == "html" or lowered.startswith("html."):
                raise ValueError(
                    "That selector points to the page root, not the clickable target. "
                    "Please provide the actual button, link, or menu item selector."
                )

        step.input["selector"] = selector
        step.input["_selector_help_original"] = requested_selector
        # Preserve the original selector target so the API can update selector_profile
        # after a successful retry (keyed by _recovery_selector_key in step.input).
        step.input["_recovery_selector_key"] = requested_selector
        step.provided_selector = selector
        step.status = StepStatus.pending
        step.started_at = None
        step.ended_at = None
        step.error = None
        step.message = "Retrying this step with the selector you provided."
        step.failure_screenshot = None
        step.user_input_kind = None
        step.user_input_prompt = None
        step.requested_selector_target = None

        # If the user is providing a selector for step N but earlier steps are still
        # in waiting_for_input, execution would break on those and never reach step N.
        # Mark them skipped (not failed) so the execution loop bypasses them cleanly.
        for earlier in run.steps[:step.index]:
            if earlier.status == StepStatus.waiting_for_input:
                LOGGER.info(
                    "Run %s: step %d superseded by selector retry on step %d — marking skipped",
                    run_id, earlier.index + 1, step.index + 1,
                )
                earlier.status = StepStatus.skipped
                earlier.user_input_kind = None
                earlier.user_input_prompt = None
                earlier.requested_selector_target = None
                earlier.message = "Skipped — user chose to retry a later step."

        # Reset any steps that were skipped due to this failure so they run
        # after the retry succeeds.
        for subsequent in run.steps[step.index + 1:]:
            if subsequent.status == StepStatus.skipped:
                subsequent.status = StepStatus.pending
                subsequent.message = None
                subsequent.error = None

        run.status = RunStatus.running
        run.finished_at = None
        self._run_store.persist(run)
        return run

    @classmethod
    def _can_accept_manual_selector_hint(cls, step: StepRuntimeState) -> bool:
        if step.status == StepStatus.waiting_for_input:
            return step.user_input_kind == "selector"
        if step.status != StepStatus.failed:
            return False
        if step.type not in {"click", "type", "select", "wait", "handle_popup", "verify_text"}:
            return False
        error_text = str(step.error or step.message or "").strip()
        if not error_text:
            return False
        probe = RuntimeError(error_text)
        return cls._should_request_selector_help(step, probe)

    @staticmethod
    def _editable_selector_field(step: StepRuntimeState) -> str | None:
        payload = step.input or {}
        for field in ("selector", "source_selector", "target_selector"):
            value = payload.get(field)
            if isinstance(value, str) and value.strip():
                return field
        return None

    @staticmethod
    def _step_text_hint(step_input: dict[str, Any]) -> str | None:
        for field in ("text_hint", "value", "text"):
            value = step_input.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        target = step_input.get("target")
        if isinstance(target, dict):
            for field in ("text", "label", "placeholder", "context", "kind", "role"):
                value = target.get(field)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    @staticmethod
    def _selector_seed_from_target(step_input: dict[str, Any], step_type: str) -> str:
        target = step_input.get("target")
        if not isinstance(target, dict):
            return ""
        for field in ("text", "label", "placeholder", "context"):
            value = target.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        role = target.get("role")
        kind = target.get("kind")
        pieces = []
        if isinstance(kind, str) and kind.strip():
            pieces.append(kind.strip())
        if isinstance(role, str) and role.strip():
            pieces.append(role.strip())
        return " ".join(pieces).strip()

    @staticmethod
    def _intent_ordinal(value: str) -> int | None:
        lowered = value.lower()
        word_map = {
            "first": 1,
            "second": 2,
            "third": 3,
            "fourth": 4,
            "fifth": 5,
        }
        for word, ordinal in word_map.items():
            if word in lowered:
                return ordinal
        numeric = re.search(r"\b(\d+)(?:st|nd|rd|th)?\b", lowered)
        if numeric:
            try:
                parsed = int(numeric.group(1))
            except Exception:
                return None
            return parsed if parsed > 0 else None
        amazon_result = re.search(r"search-results[_-](\d+)", lowered)
        if amazon_result:
            try:
                parsed = int(amazon_result.group(1))
            except Exception:
                return None
            return parsed if parsed > 0 else None
        return None

    @staticmethod
    def _intent_scope_hint(value: str) -> str | None:
        lowered = value.lower()
        for scope in ("form", "main", "nav", "article", "header", "footer", "dialog"):
            if scope in lowered:
                return scope
        return None

    def _intent_target_text(self, step_type: str, raw_selector: str, text_hint: str | None) -> str | None:
        explicit = self._extract_selector_text(raw_selector)
        if explicit:
            return explicit

        lowered = raw_selector.strip().lower()
        alias_match = re.search(r"\{\{\s*selector\.([a-z0-9_.-]+)\s*\}\}", lowered)
        if alias_match:
            alias_text = alias_match.group(1).replace(".", " ").replace("_", " ").replace("-", " ").strip()
            if alias_text:
                return alias_text
        for phrase in (
            "add to cart",
            "search",
            "login",
            "log in",
            "sign in",
            "play",
            "submit",
            "contents",
            "product title",
            "video title",
        ):
            if phrase in lowered:
                return phrase

        if step_type == "verify_text" and text_hint and text_hint.strip():
            return text_hint.strip()

        cleaned = re.sub(r"[\[\]#.:>'\"=_()-]+", " ", lowered)
        tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", cleaned)
            if token not in {
                "selector",
                "input",
                "button",
                "click",
                "type",
                "wait",
                "text",
                "role",
                "main",
                "form",
                "list",
                "item",
                "link",
                "visible",
                "hidden",
                "submit",
                "type",
                "name",
                "data",
                "component",
                "result",
                "results",
            }
        ]
        if not tokens:
            return None
        return " ".join(tokens[:4]).strip() or None

    def _intent_element_type(self, step_type: str, raw_selector: str, text_hint: str | None) -> str:
        lowered = " ".join(part for part in (raw_selector.lower(), (text_hint or "").lower()) if part).strip()
        if step_type in {"type", "select"}:
            return "input"
        if any(token in lowered for token in ("s-search-result", "data-asin", "product card", "video", "result", "listitem", "article", "feed")):
            return "listitem"
        if any(token in lowered for token in ("link", "href", " h2 a", " a[", "role='link'", 'role="link"', "title link")):
            return "link"
        if any(token in lowered for token in ("button", "submit", "login", "log in", "sign in", "play", "add to cart", "search button")):
            return "button"
        if any(token in lowered for token in ("input", "textbox", "searchbox", "textarea", "placeholder", "name='q'", 'name="q"')):
            return "input"
        return "any"

    def _build_step_intent(self, step_type: str, raw_selector: str | None, text_hint: str | None = None) -> StepIntent:
        selector = (raw_selector or "").strip()
        # Prefer clean visible text over raw CSS selector syntax when computing
        # ordinal/scope signals — raw selectors pollute intent extraction.
        extracted_text = self._extract_selector_text(selector) if selector else None
        source = " ".join(part for part in (extracted_text or selector, text_hint or "") if part).strip()
        return StepIntent(
            action=step_type,
            element_type=self._intent_element_type(step_type, selector, text_hint),
            target_text=self._intent_target_text(step_type, selector, text_hint),
            ordinal=self._intent_ordinal(source),
            scope_hint=self._intent_scope_hint(source),
            raw_selector=selector or None,
            text_hint=text_hint,
        )

    @staticmethod
    def _serialize_step_intent(intent: StepIntent | None) -> dict[str, Any] | None:
        if intent is None:
            return None
        return {
            "action": intent.action,
            "element_type": intent.element_type,
            "target_text": intent.target_text,
            "ordinal": intent.ordinal,
            "scope_hint": intent.scope_hint,
            "raw_selector": intent.raw_selector,
            "text_hint": intent.text_hint,
        }

    def _selector_help_mode(self) -> str:
        return str(getattr(self._settings, "selector_help_mode", "fail")).strip().lower()

    def _selector_llm_recovery_enabled(self) -> bool:
        return bool(getattr(self._settings, "selector_llm_recovery_enabled", True))

    def _selector_llm_max_candidates(self) -> int:
        configured = int(getattr(self._settings, "selector_llm_max_candidates", 3))
        return max(configured, 1)

    async def _capture_failure_screenshot(self, run_id: str, step: StepRuntimeState) -> None:
        try:
            screenshot_name = f"step-{step.index:03d}-failed.png"
            screenshot_bytes = await self._browser.capture_screenshot()
            await self._files.write_bytes_artifact(run_id, screenshot_name, screenshot_bytes)
            step.failure_screenshot = screenshot_name
        except Exception as exc:
            LOGGER.warning(
                "Failed to capture screenshot for run %s step %s: %s",
                run_id,
                step.index,
                self._compact_error(exc),
            )

    async def _run_failure_diagnosis(self, run: "RunState", step: StepRuntimeState) -> None:
        """
        After a screenshot is captured, calls the brain vision endpoint to produce:
          - step.failure_diagnosis: plain-English explanation of what went wrong
          - step.failure_suggested_fix: one-sentence suggestion for fixing the step
          - step.failure_selector_suggestions: LLM-suggested selectors to try manually
        Failures here are non-fatal — diagnosis is best-effort.
        """
        if not step.failure_screenshot:
            return
        try:
            screenshot_bytes = await self._files.read_bytes_artifact(run.run_id, step.failure_screenshot)
            if not screenshot_bytes:
                return
            import base64
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode()

            diagnosis_result = await self._brain.diagnose_failure(
                step_type=step.type,
                error_message=str(step.error or step.message or "unknown error"),
                screenshot_base64=screenshot_b64,
                goal=run.prompt or None,
            )
            step.failure_diagnosis = diagnosis_result.get("diagnosis") or None
            step.failure_suggested_fix = diagnosis_result.get("suggested_fix") or None
            LOGGER.info(
                "Run %s step %d: failure diagnosis complete (%r)",
                run.run_id, step.index + 1,
                (step.failure_diagnosis or "")[:80],
            )
        except Exception as exc:
            LOGGER.warning(
                "Run %s step %d: failure diagnosis failed (%s)",
                run.run_id, step.index + 1, self._compact_error(exc),
            )

        # Selector suggestions — reuse existing suggest_selectors brain call
        try:
            raw_selector = str((step.input or {}).get("selector", "")).strip()
            if raw_selector and step.type in {"click", "type", "select", "verify_text"}:
                page_snapshot = await self._safe_page_snapshot() or {}
                suggestions = await self._brain.suggest_selectors(
                    step_type=step.type,
                    failed_selector=raw_selector,
                    error_message=str(step.error or step.message or "unknown error"),
                    page=page_snapshot,
                    text_hint=self._step_text_hint(step.input or {}),
                    max_candidates=3,
                )
                if suggestions:
                    step.failure_selector_suggestions = suggestions
                    LOGGER.info(
                        "Run %s step %d: %d selector suggestions generated",
                        run.run_id, step.index + 1, len(suggestions),
                    )
        except Exception as exc:
            LOGGER.warning(
                "Run %s step %d: selector suggestions failed (%s)",
                run.run_id, step.index + 1, self._compact_error(exc),
            )

    async def _verify_page_transition(
        self,
        expected_signal: str | None,
        timeout_seconds: float = 8.0,
    ) -> bool:
        if not expected_signal:
            return True
        try:
            await asyncio.wait_for(
                self._browser.wait_for(
                    until="selector_visible",
                    ms=int(timeout_seconds * 1000),
                    selector=expected_signal,
                    load_state=None,
                ),
                timeout=timeout_seconds + 1.0,
            )
            return True
        except Exception:
            return False

    async def _dispatch_step(self, run: RunState, raw_step: dict) -> str:
        step_type = raw_step.get("type")
        test_data = run.test_data or {}
        selector_profile = run.selector_profile or {}
        run_domain = self._extract_run_domain(run)

        if step_type == "navigate":
            target_url = self._apply_template(str(raw_step["url"]), test_data, run_id=run.run_id)
            result = await self._browser.navigate(target_url)
            try:
                await asyncio.wait_for(
                    self._browser.wait_for(
                        until="selector_visible",
                        ms=5000,
                        selector="body *:not(script):not(style)",
                        load_state=None,
                    ),
                    timeout=6.0,
                )
            except Exception:
                pass
            return result

        if step_type == "click":
            selector = str(raw_step.get("selector") or self._selector_seed_from_target(raw_step, step_type))
            alias_key = self._selector_alias_key(selector)
            text_hint = raw_step.get("text_hint")
            if alias_key == "transition_canvas_label":
                try:
                    await self._run_with_selector_fallback(
                        "{{selector.save_changes_button}}",
                        "wait",
                        selector_profile,
                        test_data,
                        run_domain,
                        lambda resolved: self._browser.wait_for(
                            until="selector_visible",
                            ms=1500,
                            selector=resolved,
                            load_state=None,
                        ),
                    )
                    return "Transition canvas click treated as non-blocking"
                except Exception:
                    if text_hint is not None:
                        for label_selector in self._transition_label_signal_selectors(str(text_hint), test_data):
                            try:
                                await self._run_with_selector_fallback(
                                    label_selector,
                                    "wait",
                                    selector_profile,
                                    test_data,
                                    run_domain,
                                    lambda resolved: self._browser.wait_for(
                                        until="selector_visible",
                                        ms=1500,
                                        selector=resolved,
                                        load_state=None,
                                    ),
                                )
                                return "Transition label is visible on canvas"
                            except Exception:
                                continue
                    return "Transition canvas click treated as non-blocking"
            try:
                click_operation = self._run_with_selector_fallback(
                    selector,
                    step_type,
                    selector_profile,
                    test_data,
                    run_domain,
                    lambda resolved: self._browser.click(resolved),
                    text_hint=str(text_hint) if text_hint is not None else None,
                    pre_attempt=lambda resolved: self._capture_click_pre_state(resolved),
                    post_validate=lambda resolved, _result, before: self._validate_click_post_action(
                        resolved_selector=resolved,
                        before_snapshot=before,
                        raw_selector=selector,
                        text_hint=str(text_hint) if text_hint is not None else None,
                    ),
                    grounded_selector=raw_step.get("_grounded_selector"),
                )
                if alias_key in {"login_button", "transition_canvas_label"}:
                    click_budget_s = max(
                        3.0,
                        min(float(self._settings.step_timeout_seconds) * 0.2, 12.0),
                    )
                    result = await asyncio.wait_for(click_operation, timeout=click_budget_s)
                else:
                    result = await click_operation
                if alias_key == "login_button":
                    try:
                        await asyncio.wait_for(
                            self._browser.wait_for(
                                until="selector_visible",
                                ms=8000,
                                selector=(
                                    "[href*='dashboard'], "
                                    "nav:not(:has([href*='login'])), "
                                    "[aria-label*='logout'], "
                                    "[aria-label*='sign out'], "
                                    "button:has-text('Logout'), "
                                    "button:has-text('Sign Out')"
                                ),
                                load_state=None,
                            ),
                            timeout=10.0,
                        )
                    except Exception:
                        try:
                            await asyncio.wait_for(
                                self._browser.wait_for(
                                    until="selector_hidden",
                                    ms=3000,
                                    selector=(
                                        "input[type='password'], "
                                        "button:has-text('Sign In'), "
                                        "button:has-text('Login')"
                                    ),
                                    load_state=None,
                                ),
                                timeout=4.0,
                            )
                        except Exception:
                            pass
                return result
            except Exception as exc:
                if alias_key == "login_button":
                    # Do not treat later visibility checks as proof that the login click
                    # itself succeeded. The explicit next wait/verify step must confirm
                    # post-login state, otherwise we can silently continue on the wrong page.
                    raise exc
                if alias_key == "transition_canvas_label" and text_hint is not None:
                    for label_selector in self._transition_label_signal_selectors(str(text_hint), test_data):
                        try:
                            await self._run_with_selector_fallback(
                                label_selector,
                                "wait",
                                selector_profile,
                                test_data,
                                run_domain,
                                lambda resolved: self._browser.wait_for(
                                    until="selector_visible",
                                    ms=8000,
                                    selector=resolved,
                                    load_state=None,
                                ),
                            )
                            return "Transition label is visible on canvas"
                        except Exception:
                            continue
                    raise exc
                raise

        if step_type == "type":
            selector = str(raw_step.get("selector") or self._selector_seed_from_target(raw_step, step_type))
            raw_text = str(raw_step["text"])
            text = self._apply_template(raw_text, test_data, run_id=run.run_id)
            clear_first = bool(raw_step.get("clear_first", True))
            if "{{random" in raw_text.lower() or "random" in raw_text.lower():
                selector_field_name = self._run_context_selector_field_name(selector)
                run_context = self._run_context.setdefault(run.run_id, {})
                run_context["last_typed_value"] = text
                if selector_field_name:
                    run_context[f"last_typed_{selector_field_name}"] = text
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
                post_validate=lambda resolved, _result, _pre: self._validate_type_post_action(
                    resolved_selector=resolved,
                    expected_text=text,
                    clear_first=clear_first,
                ),
                grounded_selector=raw_step.get("_grounded_selector"),
            )

        if step_type == "select":
            selector = str(raw_step.get("selector") or self._selector_seed_from_target(raw_step, step_type))
            value = self._apply_template(str(raw_step["value"]), test_data, run_id=run.run_id)
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
                post_validate=lambda resolved, _result, _pre: self._validate_select_post_action(
                    resolved_selector=resolved,
                    expected_value=value,
                ),
                grounded_selector=raw_step.get("_grounded_selector"),
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
                raw_selector = str(selector)
                alias_key = self._selector_alias_key(raw_selector)
                try:
                    return await self._run_with_selector_fallback(
                        raw_selector,
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
                except Exception as exc:
                    if alias_key == "workflow_saved_success":
                        try:
                            await self._run_with_selector_fallback(
                                "{{selector.cancel_button}}",
                                "wait",
                                selector_profile,
                                test_data,
                                run_domain,
                                lambda resolved: self._browser.wait_for(
                                    until="selector_visible",
                                    ms=8000,
                                    selector=resolved,
                                    load_state=None,
                                ),
                            )
                            return "Workflow editor remained available after save"
                        except Exception:
                            raise exc
                    raise

            if until == "timeout":
                return await self._smart_stabilization_wait(int(ms) if ms is not None else 500)

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
            selector = str(raw_step.get("selector") or self._selector_seed_from_target(raw_step, step_type))
            match = str(raw_step.get("match", "contains"))
            value = self._apply_template(str(raw_step["value"]), test_data, run_id=run.run_id)
            value_lower = value.lower()
            if "same as" in value_lower or "what we entered" in value_lower:
                run_context = self._run_context.get(run.run_id, {})
                selector_field_name = self._run_context_selector_field_name(selector)
                if selector_field_name:
                    value = str(
                        run_context.get(
                            f"last_typed_{selector_field_name}",
                            run_context.get("last_typed_value", value),
                        )
                    )
                else:
                    value = str(run_context.get("last_typed_value", value))
            if selector.strip().lower() in {"h1", "h2", "h3", "body", "main", "form"}:
                return await self._browser.verify_text(
                    selector=selector,
                    match=match,
                    value=value,
                )
            # When the user has manually provided a selector, use it directly
            # without the fallback pipeline. The pipeline generates its own candidates
            # and ignores the user's choice, causing an infinite re-pause loop.
            # _selector_help_original is only set when apply_manual_selector_hint() runs.
            if raw_step.get("_selector_help_original"):
                return await self._browser.verify_text(
                    selector=selector,
                    match=match,
                    value=value,
                )
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
                self._apply_template(str(baseline_path), test_data, run_id=run.run_id)
                if baseline_path is not None
                else None
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

    async def _safe_page_snapshot(self) -> dict[str, Any] | None:
        try:
            return await self._browser.inspect_page(include_screenshot=False)
        except Exception:
            return None

    @staticmethod
    def _snapshot_item_summary(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "tag": str(item.get("tag", "")).strip()[:40],
            "role": str(item.get("role", "")).strip()[:40],
            "text": str(item.get("text", "")).strip()[:80],
            "aria": str(item.get("aria", "")).strip()[:80],
            "id": str(item.get("id", "")).strip()[:80],
            "name": str(item.get("name", "")).strip()[:80],
            "scope": str(item.get("scope", "")).strip()[:40],
            "visible": bool(item.get("visible", True)),
            "enabled": bool(item.get("enabled", True)),
        }

    def _summarize_page_state(self, snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(snapshot, dict):
            return None
        interactive_elements = snapshot.get("interactive_elements")
        visible_count = 0
        sample: list[dict[str, Any]] = []
        if isinstance(interactive_elements, list):
            for item in interactive_elements:
                if not isinstance(item, dict):
                    continue
                if item.get("visible", True):
                    visible_count += 1
                if len(sample) < 5:
                    sample.append(self._snapshot_item_summary(item))
        return {
            "url": str(snapshot.get("url", ""))[:240],
            "title": str(snapshot.get("title", ""))[:160],
            "text_excerpt": str(snapshot.get("text_excerpt", ""))[:240],
            "visible_interactive_count": visible_count,
            "page_count": int(snapshot.get("page_count") or 1),
            "interactive_sample": sample,
        }

    @staticmethod
    def _looks_like_explicit_selector(selector: str) -> bool:
        normalized = selector.strip()
        if not normalized:
            return False
        if normalized.startswith("{{selector.") and normalized.endswith("}}"):
            return False
        lowered = normalized.lower()
        if "," in normalized:
            return False
        if any(token in lowered for token in (":has-text(", ":text-is(", ":nth-of-type(", ":first-", " >> ", "nth=")):
            return False
        if lowered.startswith(("text=", "{{selector.")):
            return False
        if lowered.startswith("xpath="):
            return True
        if lowered.startswith("#") and " " not in normalized:
            return True
        simple_prefixes = ("input", "button", "select", "textarea", "a", "form")
        if lowered.startswith(simple_prefixes):
            return normalized.count(" ") <= 1
        if lowered.startswith(("[", ".")):
            return " " not in normalized
        return False

    # ------------------------------------------------------------------
    # Pre-action page health check
    # ------------------------------------------------------------------

    # Patterns that strongly suggest the browser landed on an HTTP error
    # page rather than the intended target.  Checked against page title
    # first (more reliable) then text_excerpt (only when element count is
    # very low, to avoid false positives on pages that legitimately mention
    # these terms).
    _ERROR_PAGE_TITLE_PATTERNS: tuple[tuple[str, str], ...] = (
        ("404", "http_404"),
        ("403", "http_403"),
        ("401", "http_401"),
        ("500", "http_500"),
        ("502", "http_502"),
        ("503", "http_503"),
        ("not found", "page_not_found"),
        ("page not found", "page_not_found"),
        ("access denied", "access_denied"),
        ("forbidden", "forbidden"),
        ("internal server error", "server_error"),
        ("bad gateway", "http_502"),
        ("service unavailable", "http_503"),
        ("unauthorized", "http_401"),
    )
    _ERROR_BROWSER_PATTERNS: tuple[tuple[str, str], ...] = (
        ("err_name_not_resolved", "dns_error"),
        ("err_connection_refused", "connection_refused"),
        ("err_connection_timed_out", "connection_timeout"),
        ("this site can't be reached", "connection_error"),
        ("this page isn't working", "page_error"),
        ("net::err_", "network_error"),
    )

    @staticmethod
    def _check_page_health(
        snapshot: dict[str, Any] | None,
        run: RunState,
        step: StepRuntimeState,
    ) -> dict[str, Any]:
        """
        Inspect the page snapshot for conditions that will make any selector
        attempt pointless or misleading.  Returns a structured health report:

            {
                "status": "ok" | "warn" | "block",
                "issues": [{"type": "<issue_type>", "detail": "<human message>"}]
            }

        ``block``  — a hard issue (error page, critical domain mismatch).
                     The caller should raise and skip selector attempts.
        ``warn``   — a soft issue (loading indicator, unexpected domain,
                     non-cookie modal overlay).  Log and proceed; the normal
                     pipeline may still succeed.
        ``ok``     — no issues detected.
        """
        if not isinstance(snapshot, dict):
            return {"status": "ok", "issues": []}

        issues: list[dict[str, str]] = []
        current_url = str(snapshot.get("url", "")).strip()
        title = str(snapshot.get("title", "")).lower().strip()
        text_excerpt = str(snapshot.get("text_excerpt", "")).lower().strip()
        interactive_elements: list[Any] = snapshot.get("interactive_elements") or []
        visible_count = sum(
            1 for el in interactive_elements
            if isinstance(el, dict) and el.get("visible", True)
        )

        # ---- 1. Error page detection ----------------------------------------
        # Check title first (most reliable signal).
        for pattern, code in AgentExecutor._ERROR_PAGE_TITLE_PATTERNS:
            if pattern in title:
                issues.append({
                    "type": "error_page",
                    "detail": (
                        f"Page title suggests an HTTP error ({code}): {title!r} — "
                        f"URL: {current_url}"
                    ),
                })
                break

        # Check for browser-level error messages (DNS/network errors).
        if not issues:
            combined = title + " " + text_excerpt
            for pattern, code in AgentExecutor._ERROR_BROWSER_PATTERNS:
                if pattern in combined:
                    issues.append({
                        "type": "browser_error",
                        "detail": (
                            f"Browser error detected ({code}): {title!r} — "
                            f"URL: {current_url}"
                        ),
                    })
                    break

        # Check text_excerpt only when the page is nearly empty (low element
        # count), making it very likely to be a dedicated error page.
        if not issues and visible_count <= 3 and text_excerpt:
            for pattern, code in AgentExecutor._ERROR_PAGE_TITLE_PATTERNS:
                if pattern in text_excerpt:
                    issues.append({
                        "type": "error_page",
                        "detail": (
                            f"Near-empty page with error text ({code}): {text_excerpt[:120]!r} — "
                            f"URL: {current_url}"
                        ),
                    })
                    break

        # ---- 2. Domain mismatch (warn only) ---------------------------------
        expected_url = (run.start_url or "").strip()
        if current_url and expected_url:
            current_domain = urlparse(current_url).netloc.lower()
            expected_domain = urlparse(expected_url).netloc.lower()
            # Strip leading "www." for comparison
            current_root = current_domain.lstrip("www.")
            expected_root = expected_domain.lstrip("www.")
            if (
                current_root
                and expected_root
                and current_root != expected_root
                # Allow subdomains of the expected root
                and not current_root.endswith("." + expected_root)
                and not expected_root.endswith("." + current_root)
            ):
                issues.append({
                    "type": "domain_mismatch",
                    "detail": (
                        f"Current page domain {current_domain!r} does not match "
                        f"expected domain {expected_domain!r} — "
                        f"current URL: {current_url}"
                    ),
                })

        # ---- 3. Blocking modal / overlay detection (warn only) --------------
        # Look for dialog/alertdialog roles that are NOT cookie consent
        # popups (those are already handled elsewhere).
        cookie_signals = {"cookie", "cookies", "consent", "privacy", "gdpr"}
        for el in interactive_elements[:40]:
            if not isinstance(el, dict):
                continue
            if not el.get("visible", True):
                continue
            role = str(el.get("role", "")).lower()
            if role not in {"dialog", "alertdialog"}:
                continue
            el_text = str(el.get("text", "") or el.get("aria", "")).lower()
            if any(s in el_text for s in cookie_signals):
                continue  # already handled by popup-blocker path
            issues.append({
                "type": "modal_overlay",
                "detail": (
                    f"A blocking modal/dialog is open (role={role!r} text={el_text[:80]!r}) "
                    f"which may intercept interaction with the target element."
                ),
            })
            break  # one warning is enough

        # ---- 4. Loading state detection (warn only) -------------------------
        loading_signals = ("loading", "please wait", "spinner", "skeleton")
        if visible_count == 0 and any(s in title for s in loading_signals):
            issues.append({
                "type": "loading_state",
                "detail": (
                    f"Page appears to still be loading (title={title!r}, "
                    f"visible_elements={visible_count})"
                ),
            })

        # ---- Determine overall status ---------------------------------------
        # "error_page" and "browser_error" are hard blockers.
        # Everything else is a soft warning.
        blocking_types = {"error_page", "browser_error"}
        has_block = any(i["type"] in blocking_types for i in issues)
        status = "block" if has_block else ("warn" if issues else "ok")

        return {"status": status, "issues": issues}

    async def _assert_step_page_state(
        self,
        run: RunState,
        step: StepRuntimeState,
        snapshot: dict[str, Any] | None,
        intent: StepIntent | None,
    ) -> dict[str, Any]:
        if step.type not in {"click", "type", "select"}:
            return {"status": "skipped", "reason": "step_type_not_guarded"}
        if not isinstance(snapshot, dict):
            return {"status": "skipped", "reason": "snapshot_unavailable"}
        if self._looks_like_popup_blocker(snapshot):
            return {"status": "skipped", "reason": "popup_blocker_detected"}

        raw_selector = str(step.input.get("selector", "")).strip()
        if not raw_selector:
            return {"status": "skipped", "reason": "missing_selector"}
        if self._looks_like_explicit_selector(raw_selector):
            return {"status": "skipped", "reason": "explicit_selector"}
        selector_alias = self._selector_alias_key(raw_selector)

        target_terms = self._selector_search_terms(
            raw_selector,
            self._step_text_hint(step.input),
            intent,
        )
        interactive_elements = snapshot.get("interactive_elements")
        matched_elements: list[dict[str, Any]] = []
        if target_terms and isinstance(interactive_elements, list):
            for item in interactive_elements:
                if not isinstance(item, dict):
                    continue
                if not self._snapshot_item_matches_intent(item, intent or self._build_step_intent(step.type, raw_selector)):
                    continue
                haystack = " ".join(
                    str(item.get(field, "")).lower()
                    for field in ("text", "aria", "name", "id", "testid", "placeholder", "title", "href")
                )
                if any(term in haystack for term in target_terms):
                    matched_elements.append(item)
        if target_terms and not matched_elements:
            # Before giving up, check whether the intent text appears anywhere in
            # the page's visible text content.  This handles display elements
            # (divs, headings, paragraphs) that are valid click targets but
            # never appear in interactive_elements.
            text_excerpt = str(snapshot.get("text_excerpt", "")).lower()
            if any(term in text_excerpt for term in target_terms):
                LOGGER.debug(
                    "Page assertion: intent terms %s not in interactive_elements "
                    "but found in text_excerpt — treating as display element, proceeding.",
                    target_terms,
                )
                return {
                    "status": "passed",
                    "reason": "intent_found_in_page_text",
                    "matched_terms": [t for t in target_terms if t in text_excerpt],
                }
            intent_label = (intent.target_text if intent else None) or raw_selector
            raise ValueError(
                f"Page state assertion failed before {step.type}: "
                f"no element matched intent '{intent_label}' on the current page."
            )

        candidates = self._page_snapshot_selector_candidates(
            snapshot,
            raw_selector,
            step.type,
            self._step_text_hint(step.input),
        )
        if candidates:
            return {
                "status": "passed",
                "reason": "matching_live_candidates_found",
                "candidate_count": len(candidates),
                "top_candidates": candidates[:3],
            }

        intent_label = (intent.target_text if intent else None) or raw_selector
        if selector_alias or (intent and intent.target_text):
            raise ValueError(
                f"Page state assertion failed before {step.type}: "
                f"no live selector candidates matched intent '{intent_label}'."
            )
        return {
            "status": "warn",
            "reason": "no_snapshot_candidates",
            "intent": intent_label,
        }

    async def _verify_step_outcome_against_page_state(
        self,
        run: RunState,
        step: StepRuntimeState,
        message: str,
        *,
        before_snapshot: dict[str, Any] | None,
        click_pre_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        after_snapshot = await self._safe_page_snapshot()
        verification: dict[str, Any] = {
            "status": "skipped",
            "reason": "no_additional_verification",
            "after_state": self._summarize_page_state(after_snapshot),
        }

        if step.type in {"click", "type", "select"} and "post_validation=passed" in str(message):
            verification["status"] = "passed"
            verification["reason"] = "dispatcher_post_validation"
            return verification

        if step.type == "navigate":
            target_url = self._apply_template(str(step.input.get("url", "")), run.test_data or {}, run_id=run.run_id)
            actual_url = str((after_snapshot or {}).get("url", "")).strip()
            if target_url and actual_url and (
                actual_url == target_url
                or actual_url.startswith(target_url)
                or target_url.startswith(actual_url)
            ):
                verification["status"] = "passed"
                verification["reason"] = "target_url_reached"
                return verification
            raise ValueError(
                f"Navigate verification failed: expected page near '{target_url}' but reached '{actual_url or '<empty>'}'."
            )

        if step.type == "click" and step.provided_selector:
            _click_target_context = (
                click_pre_state.get("element_context") if click_pre_state else None
            )
            assessment = await self._browser.assess_click_effect(
                selector=step.provided_selector,
                before_snapshot=before_snapshot,
                raw_selector=str(step.input.get("selector", "")) or None,
                text_hint=self._step_text_hint(step.input),
                target_context=_click_target_context,
            )
            verification["assessment"] = assessment
            if str(assessment.get("status", "")).strip().lower() == "passed":
                verification["status"] = "passed"
                verification["reason"] = "click_effect_observed"
                return verification
            raise ValueError(f"Click verification failed: {assessment.get('detail') or assessment}")

        if step.type == "type" and step.provided_selector:
            detail = await self._validate_type_post_action(
                resolved_selector=step.provided_selector,
                expected_text=self._apply_template(
                    str(step.input.get("text", "")),
                    run.test_data or {},
                    run_id=run.run_id,
                ),
                clear_first=bool(step.input.get("clear_first", True)),
            )
            verification["status"] = "passed"
            verification["reason"] = "typed_value_verified"
            verification["detail"] = detail
            return verification

        if step.type == "select" and step.provided_selector:
            detail = await self._validate_select_post_action(
                resolved_selector=step.provided_selector,
                expected_value=self._apply_template(
                    str(step.input.get("value", "")),
                    run.test_data or {},
                    run_id=run.run_id,
                ),
            )
            verification["status"] = "passed"
            verification["reason"] = "selected_value_verified"
            verification["detail"] = detail
            return verification

        return verification

    async def _capture_click_pre_state(self, resolved_selector: str) -> dict[str, Any]:
        page_snapshot = await self._safe_page_snapshot()
        try:
            element_context = await asyncio.wait_for(
                self._browser.get_element_context(resolved_selector),
                timeout=1.0,
            )
        except Exception:
            element_context = None
        return {
            "page_snapshot": page_snapshot,
            "element_context": element_context,
        }

    async def _validate_click_post_action(
        self,
        *,
        resolved_selector: str,
        before_snapshot: dict[str, Any] | None,
        raw_selector: str | None = None,
        text_hint: str | None = None,
    ) -> str:
        page_snapshot = before_snapshot
        target_context = None
        if isinstance(before_snapshot, dict) and (
            "page_snapshot" in before_snapshot or "element_context" in before_snapshot
        ):
            page_snapshot = before_snapshot.get("page_snapshot")
            target_context = before_snapshot.get("element_context")
        mismatch_detail = self._detect_click_target_mismatch(
            resolved_selector=resolved_selector,
            raw_selector=raw_selector,
            text_hint=text_hint,
            target_context=target_context,
        )
        if mismatch_detail:
            raise ValueError(mismatch_detail)
        assessment = await self._browser.assess_click_effect(
            selector=resolved_selector,
            before_snapshot=page_snapshot,
            raw_selector=raw_selector,
            text_hint=text_hint,
            target_context=target_context,
        )
        status = str(assessment.get("status") or "").strip().lower()
        detail = str(assessment.get("detail") or "Click effect validation completed.")
        if status == "failed":
            raise ValueError(detail)
        if status == "ambiguous":
            return f"post_validation=ambiguous ({detail})"
        return f"post_validation=passed ({detail})"

    @staticmethod
    def _salient_click_target_terms(target_text: str | None) -> list[str]:
        text = (target_text or "").strip().lower()
        if not text:
            return []
        stop = {
            "the", "and", "for", "with", "from", "that", "this", "button", "link",
            "form", "workflow", "page", "dialog", "modal", "menu", "module", "section",
            "screen", "tab", "field", "item", "list", "option",
        }
        terms: list[str] = []
        for term in re.findall(r"[a-z0-9]+", text):
            if len(term) < 4 or term in stop or term.isdigit():
                continue
            if term not in terms:
                terms.append(term)
        return terms[:3]

    @staticmethod
    def _click_target_term_variants(term: str) -> tuple[str, ...]:
        normalized = term.strip().lower()
        variants = {
            "login": ("login", "log in", "sign in", "signin"),
            "logout": ("logout", "log out", "sign out", "signout"),
            "signin": ("signin", "sign in", "login", "log in"),
            "signup": ("signup", "sign up", "register", "create account"),
            "register": ("register", "registration", "sign up", "signup", "create account"),
        }
        return variants.get(normalized, (normalized,))

    @classmethod
    def _matches_click_target_term(cls, haystack: str, term: str) -> bool:
        lowered = (haystack or "").lower()
        if not lowered:
            return False
        return any(variant in lowered for variant in cls._click_target_term_variants(term))

    def _detect_click_target_mismatch(
        self,
        *,
        resolved_selector: str,
        raw_selector: str | None,
        text_hint: str | None,
        target_context: dict[str, Any] | None,
    ) -> str | None:
        if not isinstance(target_context, dict):
            return None
        intent = self._build_step_intent("click", raw_selector or resolved_selector, text_hint)
        if intent.element_type in {"link", "listitem"}:
            return None
        expected_terms = self._salient_click_target_terms(intent.target_text)
        if not expected_terms:
            return None
        actual_haystack = " ".join(
            str(target_context.get(field, "")).lower()
            for field in ("text", "title", "aria", "href", "selector")
        ).strip()
        if not actual_haystack:
            return None
        matched = [term for term in expected_terms if self._matches_click_target_term(actual_haystack, term)]
        resolved_lower = resolved_selector.strip().lower()
        looks_link_like = any(
            token in resolved_lower
            for token in ("a:", "a[", "href", "role='link'", 'role="link"')
        )
        if matched:
            return None
        preview = str(target_context.get("text") or target_context.get("title") or target_context.get("href") or "").strip()
        if len(preview) > 80:
            preview = preview[:77] + "..."
        return (
            f"Click target mismatch: expected a target matching '{intent.target_text or raw_selector or resolved_selector}', "
            f"but resolved selector '{resolved_selector}' matched '{preview or actual_haystack[:80]}'."
        )

    async def _validate_type_post_action(
        self,
        *,
        resolved_selector: str,
        expected_text: str,
        clear_first: bool,
    ) -> str:
        actual_value = await self._browser.get_element_value(resolved_selector)
        if actual_value is None:
            raise ValueError(f"Type validation failed for {resolved_selector}: unable to read element value.")
        if clear_first:
            if actual_value != expected_text:
                raise ValueError(
                    f"Type validation failed for {resolved_selector}: expected value '{expected_text}' but found '{actual_value}'."
                )
        else:
            if not actual_value.endswith(expected_text):
                raise ValueError(
                    f"Type validation failed for {resolved_selector}: appended text '{expected_text}' not present in final value '{actual_value}'."
                )
        return f"post_validation=passed (value='{actual_value[:120]}')"

    async def _validate_select_post_action(
        self,
        *,
        resolved_selector: str,
        expected_value: str,
    ) -> str:
        actual_value = await self._browser.get_select_value(resolved_selector)
        if actual_value is None:
            raise ValueError(f"Select validation failed for {resolved_selector}: unable to read selected value.")
        if actual_value != expected_value:
            raise ValueError(
                f"Select validation failed for {resolved_selector}: expected '{expected_value}' but found '{actual_value}'."
            )
        return f"post_validation=passed (selected='{actual_value[:120]}')"

    async def _find_element_from_intent(
        self,
        step_type: str,
        raw_selector: str,
        text_hint: str | None,
        selector_profile: dict[str, list[str]],
        test_data: dict[str, Any],
        run_domain: str | None,
    ) -> list[str]:

        try:
            snapshot = await asyncio.wait_for(
                self._browser.inspect_page(include_screenshot=False),
                timeout=5.0,
            )
        except Exception:
            return []

        combined = " ".join(
            part for part in (
                raw_selector, text_hint or ""
            ) if part
        ).lower()
        combined = combined.replace("{{selector.", " ")
        combined = combined.replace("{{", " ").replace("}}", " ")
        combined = combined.replace("_", " ").replace("-", " ")
        keywords = [
            word for word in re.findall(r"[a-z0-9]+", combined)
            if len(word) >= 3 and word not in {
                "selector", "input", "button", "click",
                "type", "the", "and", "for", "wait",
                "verify", "text", "select", "form",
            }
        ]

        elements = snapshot.get("interactive_elements", [])
        if not isinstance(elements, list) or not elements:
            return []

        intent = self._build_step_intent(
            step_type, raw_selector, text_hint
        )
        scored: list[tuple[int, dict[str, Any]]] = []
        for element in elements:
            if not isinstance(element, dict):
                continue
            if not element.get("visible", True):
                continue

            haystack = " ".join(
                str(element.get(field, "")).lower()
                for field in (
                    "text", "aria", "name", "id",
                    "testid", "placeholder", "title",
                    "tag", "role", "type", "href",
                )
            )

            score = 0
            keyword_matches = 0
            for keyword in keywords:
                if keyword in haystack:
                    keyword_matches += 1
                    score += max(10, len(keyword) * 2)

            tag = str(element.get("tag", "")).lower()
            role = str(element.get("role", "")).lower()
            if step_type in {"type", "select"}:
                if tag in {"input", "textarea", "select"}:
                    score += 20
                if role in {"textbox", "combobox", "searchbox"}:
                    score += 15
            elif step_type == "click":
                if tag in {"button", "a"}:
                    score += 20
                if role in {"button", "link", "menuitem"}:
                    score += 15

            if element.get("id"):
                score += 10
            if element.get("testid"):
                score += 15
            if element.get("aria"):
                score += 8

            if not element.get("enabled", True):
                score -= 20

            if keyword_matches > 0 and score > 0 and self._snapshot_item_matches_intent(element, intent):
                scored.append((score, element))

        if not scored:
            return []

        scored.sort(key=lambda x: -x[0])
        top_elements = [elem for _, elem in scored[:5]]

        candidates: list[str] = []
        for element in top_elements:
            candidates.extend(
                self._selectors_from_snapshot_item(
                    element, step_type
                )
            )

        return self._dedupe(candidates)

    async def _run_with_selector_fallback(
        self,
        raw_selector: str,
        step_type: str,
        selector_profile: dict[str, list[str]],
        test_data: dict[str, Any],
        run_domain: str | None,
        operation: Callable[[str], Awaitable[str]],
        text_hint: str | None = None,
        pre_attempt: Callable[[str], Awaitable[Any]] | None = None,
        post_validate: Callable[[str, str, Any], Awaitable[str | None]] | None = None,
        grounded_selector: str | None = None,
    ) -> str:
        intent = self._build_step_intent(step_type, raw_selector, text_hint)
        selector_generation_started = perf_counter()
        explicit_selector = self._looks_like_explicit_selector(raw_selector)
        live_snapshot_candidates: list[str] = []
        grounded_confidence = CandidateConfidence(
            level="none",
            top_score=0,
            second_score=None,
            score_gap=0,
            retained_count=0,
        )
        live_snapshot = await self._safe_page_snapshot()
        if not explicit_selector and isinstance(live_snapshot, dict):
            live_snapshot_candidates = self._page_snapshot_selector_candidates(
                live_snapshot,
                raw_selector,
                step_type,
                text_hint,
            )
            live_snapshot_candidates, grounded_confidence = self._confidence_gate_candidates(
                live_snapshot_candidates,
                intent,
                step_type=step_type,
                source="live",
            )

        profile_candidates = self._selector_candidates(
            raw_selector,
            step_type,
            selector_profile,
            test_data,
            run_domain,
            text_hint,
        )
        selector_alias = self._selector_alias_key(raw_selector)

        grounded_failure_reason: str | None = None
        if not explicit_selector and isinstance(live_snapshot, dict) and not live_snapshot_candidates:
            grounded_failure_reason = "no_grounded_candidates"
            if selector_alias and not grounded_selector:
                alias_label = selector_alias.replace("_", " ").strip() or raw_selector
                raise ValueError(
                    f"Grounded selection failed for '{alias_label}': "
                    "the current page does not contain a matching live element."
                )

        # Slow-path stage order:
        # 1) template/profile candidates
        # 2) prepend strong live DOM candidates for non-explicit selectors
        # 3) prepend perception-grounded selector (if present)
        candidates = list(profile_candidates)
        if not explicit_selector and live_snapshot_candidates:
            prioritized_live_candidates = self._rank_live_snapshot_selectors(
                live_snapshot_candidates,
                intent,
            )
            candidates = prioritized_live_candidates + [
                c for c in candidates if c not in live_snapshot_candidates
            ]
        if grounded_selector:
            if grounded_selector not in candidates:
                candidates = [grounded_selector] + candidates
                LOGGER.debug(
                    "Selector fallback: perception selector prepended ahead of profile candidates: %r (step_type=%s)",
                    grounded_selector, step_type,
                )
            elif candidates[0] != grounded_selector:
                candidates = [grounded_selector] + [c for c in candidates if c != grounded_selector]
        if not explicit_selector and live_snapshot_candidates:
            candidate_confidence = grounded_confidence
        else:
            candidates, candidate_confidence = self._confidence_gate_candidates(
                candidates,
                intent,
                step_type=step_type,
                source="initial",
            )
        execution_policy = self._candidate_execution_policy(candidate_confidence, step_type=step_type)
        selector_generation_ms = self._elapsed_ms(selector_generation_started)
        last_error: Exception | None = None
        candidate_timeout_s = self._candidate_timeout_seconds(len(candidates), step_type=step_type)
        attempts: list[str] = []
        recovery_attempts = self._effective_selector_recovery_attempts(step_type, len(candidates))
        if execution_policy["recovery_attempts"] is not None:
            recovery_attempts = min(recovery_attempts, int(execution_policy["recovery_attempts"]))
        LOGGER.debug(
            "Selector fallback: step_type=%s raw_selector=%r domain=%r "
            "candidates=%d confidence=%s policy=%s",
            step_type, raw_selector, run_domain,
            len(candidates), candidate_confidence.level, execution_policy.get("mode"),
        )
        trace_group = self._record_step_trace_group(
            {
                "kind": "selector_fallback",
                "raw_selector": raw_selector,
                "step_type": step_type,
                "run_domain": run_domain,
                "text_hint": text_hint,
                "intent": self._serialize_step_intent(intent),
                "candidate_timeout_seconds": candidate_timeout_s,
                "recovery_attempts": recovery_attempts,
                "candidate_confidence": {
                    "level": candidate_confidence.level,
                    "top_score": candidate_confidence.top_score,
                    "second_score": candidate_confidence.second_score,
                    "score_gap": candidate_confidence.score_gap,
                    "retained_count": candidate_confidence.retained_count,
                },
                "execution_policy": execution_policy,
                "selector_generation_ms": selector_generation_ms,
                "live_candidate_lookup_ms": 0.0,
                "candidates": list(candidates),
                "attempts": [],
                "live_first_prepass": bool(grounded_selector),
                "live_first_count": 1 if grounded_selector else 0,
                "grounded_selection_enabled": not explicit_selector,
                "dom_first_mode": not explicit_selector,
                "grounded_confidence": {
                    "level": grounded_confidence.level,
                    "top_score": grounded_confidence.top_score,
                    "second_score": grounded_confidence.second_score,
                    "score_gap": grounded_confidence.score_gap,
                    "retained_count": grounded_confidence.retained_count,
                },
                "page_state_summary": self._summarize_page_state(live_snapshot),
                "grounded_failure_reason": grounded_failure_reason,
            }
        )

        for cycle in range(recovery_attempts):
            for selector in candidates:
                attempt_started = perf_counter()
                pre_state: Any = None
                pre_attempt_ms = 0.0
                browser_action_ms = 0.0
                post_validate_ms = 0.0
                # Fast-fail probe: quickly verify element is present before
                # spending the full candidate timeout attempting the action.
                # Only applies to interactive steps; skips generic operations.
                #
                # Grounded selectors (from perception / signature memory) get
                # a longer timeout because the element is known to exist —
                # it may just be slow to render after an async SPA operation.
                # All other candidates fail fast to avoid wasting time.
                if step_type in {"click", "type", "select"}:
                    is_grounded = bool(grounded_selector and selector == grounded_selector)
                    probe_timeout_ms = 3000 if is_grounded else 1500
                    probe_ok = await self._probe_element_present(selector, timeout_ms=probe_timeout_ms)
                    if not probe_ok:
                        compact_error = f"probe: element not visible within {probe_timeout_ms} ms"
                        LOGGER.warning(
                            "Selector fallback: probe MISS (cycle=%d step_type=%s selector=%r grounded=%s) — skipping",
                            cycle + 1, step_type, selector, is_grounded,
                        )
                        attempts.append(f"pass {cycle + 1}: {selector} -> {compact_error}")
                        last_error = TimeoutError(compact_error)
                        self._record_group_attempt(
                            trace_group,
                            {
                                "phase": "candidate",
                                "cycle": cycle,
                                "selector": selector,
                                "status": "probe_miss",
                                "probe_timeout_ms": probe_timeout_ms,
                                "grounded": is_grounded,
                                "elapsed_ms": self._elapsed_ms(attempt_started),
                                "error": compact_error,
                            },
                        )
                        continue
                try:
                    if pre_attempt is not None:
                        pre_started = perf_counter()
                        pre_state = await pre_attempt(selector)
                        pre_attempt_ms = self._elapsed_ms(pre_started)
                    action_started = perf_counter()
                    result = await asyncio.wait_for(operation(selector), timeout=candidate_timeout_s)
                    browser_action_ms = self._elapsed_ms(action_started)
                    validation_detail: str | None = None
                    if post_validate is not None:
                        validate_started = perf_counter()
                        validation_detail = await post_validate(selector, result, pre_state)
                        post_validate_ms = self._elapsed_ms(validate_started)
                        if validation_detail:
                            result = f"{result}; {validation_detail}"
                    self._remember_selector_success(
                        run_domain=run_domain,
                        step_type=step_type,
                        raw_selector=raw_selector,
                        resolved_selector=selector,
                        text_hint=text_hint,
                    )
                    self._remember_selector_signature(
                        run_domain=run_domain,
                        step_type=step_type,
                        raw_selector=raw_selector,
                        text_hint=text_hint,
                    )
                    active_step = self._active_step_state()
                    if active_step is not None:
                        active_step.provided_selector = selector
                    trace_group["resolved_selector"] = selector
                    trace_group["final_selected_selector"] = selector
                    trace_group["result"] = result
                    self._record_group_attempt(
                        trace_group,
                        {
                            "phase": "candidate",
                            "cycle": cycle,
                            "selector": selector,
                            "status": "success",
                            "elapsed_ms": self._elapsed_ms(attempt_started),
                            "pre_attempt_ms": pre_attempt_ms,
                            "browser_action_ms": browser_action_ms,
                            "post_validate_ms": post_validate_ms,
                            "total_attempt_ms": self._elapsed_ms(attempt_started),
                            "result": result,
                            "validation": validation_detail,
                        },
                    )
                    return result
                except Exception as exc:
                    last_error = exc
                    compact_error = self._compact_error(exc)
                    # "Execution context was destroyed" means the click triggered a
                    # page navigation — the action SUCCEEDED.  Return immediately so
                    # we don't try further candidates on a page that has already moved on.
                    if (
                        step_type == "click"
                        and "execution context was destroyed" in compact_error.lower()
                    ):
                        result = f"Clicked {selector} (navigation detected)"
                        self._remember_selector_success(
                            run_domain=run_domain,
                            step_type=step_type,
                            raw_selector=raw_selector,
                            resolved_selector=selector,
                            text_hint=text_hint,
                        )
                        self._remember_selector_signature(
                            run_domain=run_domain,
                            step_type=step_type,
                            raw_selector=raw_selector,
                            text_hint=text_hint,
                        )
                        active_step = self._active_step_state()
                        if active_step is not None:
                            active_step.provided_selector = selector
                        trace_group["resolved_selector"] = selector
                        trace_group["final_selected_selector"] = selector
                        trace_group["result"] = result
                        self._record_group_attempt(
                            trace_group,
                            {
                                "phase": "candidate",
                                "cycle": cycle,
                                "selector": selector,
                                "status": "success_navigation",
                                "elapsed_ms": self._elapsed_ms(attempt_started),
                                "browser_action_ms": browser_action_ms,
                                "total_attempt_ms": self._elapsed_ms(attempt_started),
                                "result": result,
                            },
                        )
                        return result
                    LOGGER.warning(
                        "Selector fallback: candidate FAILED (cycle=%d step_type=%s selector=%r) -> %s",
                        cycle + 1, step_type, selector, compact_error,
                    )
                    attempts.append(f"pass {cycle + 1}: {selector} -> {compact_error}")
                    self._record_group_attempt(
                        trace_group,
                        {
                            "phase": "candidate",
                            "cycle": cycle,
                            "selector": selector,
                            "status": "failed",
                            "elapsed_ms": self._elapsed_ms(attempt_started),
                            "pre_attempt_ms": pre_attempt_ms,
                            "browser_action_ms": browser_action_ms,
                            "post_validate_ms": post_validate_ms,
                            "total_attempt_ms": self._elapsed_ms(attempt_started),
                            "error": compact_error,
                        },
                    )

            if cycle >= recovery_attempts - 1:
                break
            if not self._should_retry_selector_error(last_error):
                LOGGER.debug(
                    "Selector fallback: not retrying after cycle=%d (error is not transient) step_type=%s selector=%r",
                    cycle + 1, step_type, raw_selector,
                )
                break
            LOGGER.debug(
                "Selector fallback: transient error on cycle=%d, pausing before retry. step_type=%s selector=%r",
                cycle + 1, step_type, raw_selector,
            )
            await self._selector_recovery_pause()

        live_candidates: list[str] = []
        live_candidate_lookup_ms = 0.0
        allow_live_candidates = execution_policy.get("allow_live_candidates", True)
        if self._should_skip_live_candidates(step_type, raw_selector, last_error, len(candidates)):
            allow_live_candidates = False
        if allow_live_candidates:
            LOGGER.debug(
                "Selector fallback: all initial candidates exhausted, querying live DOM. "
                "step_type=%s selector=%r attempts_so_far=%d",
                step_type, raw_selector, len(attempts),
            )
            live_lookup_started = perf_counter()

            # --- Signature Recovery -------------------------------------------
            # Before running the full live DOM re-rank, check whether we have
            # stored element signatures for this step.  If so, fetch the DOM
            # once, find elements by their semantic fingerprint, and prepend any
            # fresh selectors so they are tried first (before heuristic re-rank).
            live_snapshot: dict | None = None
            try:
                live_snapshot = await self._browser.inspect_page(include_screenshot=False)
            except Exception:
                pass

            if live_snapshot and self._selector_memory and run_domain:
                sig_lookup_keys = self._selector_memory_lookup_keys(raw_selector)
                if text_hint:
                    sig_lookup_keys.extend(self._selector_memory_lookup_keys(text_hint))
                stored_signatures: list[dict] = []
                for sig_key in self._dedupe(sig_lookup_keys):
                    stored_signatures.extend(
                        self._selector_memory.get_signatures(run_domain, step_type, sig_key, limit=3)
                    )
                if stored_signatures:
                    sig_index = build_element_index(live_snapshot)
                    sig_selectors = find_by_signatures(stored_signatures, sig_index)
                    if sig_selectors:
                        LOGGER.info(
                            "Selector fallback: signature recovery found %d fresh selector(s) "
                            "step_type=%s selector=%r",
                            len(sig_selectors), step_type, raw_selector,
                        )
                        live_candidates = [s for s in sig_selectors if s not in live_candidates]

            live_candidates_from_dom = (
                self._page_snapshot_selector_candidates(live_snapshot, raw_selector, step_type, text_hint)
                if live_snapshot and not explicit_selector
                else []
            )
            # Merge: signature-recovered selectors first, then DOM re-rank
            for sel in live_candidates_from_dom:
                if sel not in live_candidates:
                    live_candidates.append(sel)
            if grounded_selector:
                live_candidates = [sel for sel in live_candidates if sel != grounded_selector]
            live_candidate_lookup_ms = self._elapsed_ms(live_lookup_started)
            live_candidates, live_confidence = self._confidence_gate_candidates(
                live_candidates,
                intent,
                step_type=step_type,
                source="live",
            )
            LOGGER.debug(
                "Selector fallback: live DOM returned %d candidate(s) confidence=%s step_type=%s selector=%r",
                len(live_candidates), live_confidence.level, step_type, raw_selector,
            )
        else:
            LOGGER.debug(
                "Selector fallback: skipping live DOM lookup (policy=%s confidence=%s) step_type=%s selector=%r",
                execution_policy.get("mode"), candidate_confidence.level, step_type, raw_selector,
            )
            live_confidence = CandidateConfidence(level="low", top_score=0, second_score=None, score_gap=0, retained_count=0)
        trace_group["dynamic_dom_rerank"] = len(live_candidates) > 0
        trace_group["live_candidate_lookup_ms"] = live_candidate_lookup_ms
        trace_group["live_candidates"] = list(live_candidates)
        trace_group["live_candidate_confidence"] = {
            "level": live_confidence.level,
            "top_score": live_confidence.top_score,
            "second_score": live_confidence.second_score,
            "score_gap": live_confidence.score_gap,
            "retained_count": live_confidence.retained_count,
        }

        for selector in live_candidates:
            attempt_started = perf_counter()
            pre_state: Any = None
            pre_attempt_ms = 0.0
            browser_action_ms = 0.0
            post_validate_ms = 0.0
            try:
                if pre_attempt is not None:
                    pre_started = perf_counter()
                    pre_state = await pre_attempt(selector)
                    pre_attempt_ms = self._elapsed_ms(pre_started)
                action_started = perf_counter()
                result = await asyncio.wait_for(operation(selector), timeout=candidate_timeout_s)
                browser_action_ms = self._elapsed_ms(action_started)
                validation_detail: str | None = None
                if post_validate is not None:
                    validate_started = perf_counter()
                    validation_detail = await post_validate(selector, result, pre_state)
                    post_validate_ms = self._elapsed_ms(validate_started)
                    if validation_detail:
                        result = f"{result}; {validation_detail}"
                self._remember_selector_success(
                    run_domain=run_domain,
                    step_type=step_type,
                    raw_selector=raw_selector,
                    resolved_selector=selector,
                    text_hint=text_hint,
                )
                self._remember_selector_signature(
                    run_domain=run_domain,
                    step_type=step_type,
                    raw_selector=raw_selector,
                    text_hint=text_hint,
                )
                active_step = self._active_step_state()
                if active_step is not None:
                    active_step.provided_selector = selector
                trace_group["resolved_selector"] = selector
                trace_group["final_selected_selector"] = selector
                trace_group["result"] = result
                self._record_group_attempt(
                    trace_group,
                    {
                        "phase": "live_candidate",
                        "selector": selector,
                        "status": "success",
                        "elapsed_ms": self._elapsed_ms(attempt_started),
                        "pre_attempt_ms": pre_attempt_ms,
                        "browser_action_ms": browser_action_ms,
                        "post_validate_ms": post_validate_ms,
                        "total_attempt_ms": self._elapsed_ms(attempt_started),
                        "result": result,
                        "validation": validation_detail,
                    },
                )
                return result
            except Exception as exc:
                last_error = exc
                compact_error = self._compact_error(exc)
                LOGGER.warning(
                    "Selector fallback: live DOM candidate FAILED (step_type=%s selector=%r) -> %s",
                    step_type, selector, compact_error,
                )
                attempts.append(f"live: {selector} -> {compact_error}")
                self._record_group_attempt(
                    trace_group,
                    {
                        "phase": "live_candidate",
                        "selector": selector,
                        "status": "failed",
                        "elapsed_ms": self._elapsed_ms(attempt_started),
                        "pre_attempt_ms": pre_attempt_ms,
                        "browser_action_ms": browser_action_ms,
                        "post_validate_ms": post_validate_ms,
                        "total_attempt_ms": self._elapsed_ms(attempt_started),
                        "error": compact_error,
                    },
                )

        if last_error:
            active_step = self._active_step_state()
            allow_llm = execution_policy.get("allow_llm_recovery", True)
            if allow_llm and active_step is not None and not self._active_step_has_llm_recovery_group():
                LOGGER.warning(
                    "Selector fallback: all candidates exhausted (%d attempts), attempting LLM recovery. "
                    "step_type=%s raw_selector=%r last_error=%s",
                    len(attempts), step_type, raw_selector, self._compact_error(last_error),
                )
                synthetic_run = SimpleNamespace(
                    run_id="inline-selector-recovery",
                    test_data=test_data,
                    selector_profile=selector_profile,
                    start_url=None,
                    steps=[],
                )
                recovered = await self._try_llm_selector_recovery(
                    synthetic_run,
                    active_step,
                    last_error,
                    run_domain=run_domain,
                )
                if recovered:
                    return recovered
            # Build root-cause chain
            cause_chain: list[str] = []
            _walk: BaseException | None = last_error
            while _walk is not None:
                cause_chain.append(f"{type(_walk).__name__}: {_walk}")
                _next = getattr(_walk, "__cause__", None) or getattr(_walk, "__context__", None)
                _walk = _next if _next is not _walk else None
            root_cause_str = " <- ".join(cause_chain)
            LOGGER.error(
                "Selector fallback: ALL candidates FAILED. step_type=%s raw_selector=%r domain=%r "
                "total_attempts=%d confidence=%s policy=%s root_cause=%s attempted=[%s]",
                step_type, raw_selector, run_domain,
                len(attempts), candidate_confidence.level, execution_policy.get("mode"),
                root_cause_str,
                "; ".join(attempts[-5:]),  # last 5 so logs don't explode
            )
            trace_group["final_error"] = self._compact_error(last_error)
            if attempts:
                attempted = "; ".join(attempts)
                raise ValueError(f"All selector candidates failed: {attempted}") from last_error
            raise last_error
        raise ValueError(f"No valid selector candidates for: {raw_selector}")
    
    async def _live_page_selector_candidates(
        self,
        *,
        raw_selector: str,
        step_type: str,
        text_hint: str | None,
    ) -> list[str]:
        if step_type not in {"click", "type", "select", "wait", "verify_text", "handle_popup"}:
            return []
        try:
            snapshot = await self._browser.inspect_page(include_screenshot=False)
        except Exception:
            return []
        return self._page_snapshot_selector_candidates(snapshot, raw_selector, step_type, text_hint)

    def _should_skip_live_candidates(
        self,
        step_type: str,
        raw_selector: str,
        last_error: Exception | None,
        candidate_count: int,
    ) -> bool:
        selector_lower = raw_selector.strip().lower()
        if self._is_dynamic_dom_error(last_error):
            return False
        if step_type in {"type", "select"} and candidate_count <= 8:
            return True
        if step_type == "click" and candidate_count <= 6:
            if any(
                token in selector_lower
                for token in (
                    "search",
                    "submit",
                    "textbox",
                    "input",
                    "button",
                    "login",
                    "add-to-cart",
                    "cart",
                )
            ):
                return True
        if last_error is not None and not self._should_retry_selector_error(last_error):
            return True
        return False

    def _is_dynamic_dom_error(self, error: Exception | None) -> bool:
        if error is None:
            return False
        text = self._compact_error(error).lower()
        markers = (
            "element is not attached",
            "element is detached",
            "stale",
            "execution context was destroyed",
            "strict mode violation",
            "target closed",
            "navigation",
            "another element would receive the click",
        )
        return any(marker in text for marker in markers)

    def _page_snapshot_selector_candidates(
        self,
        snapshot: dict[str, Any],
        raw_selector: str,
        step_type: str,
        text_hint: str | None,
    ) -> list[str]:
        intent = self._build_step_intent(step_type, raw_selector, text_hint)
        elements = snapshot.get("interactive_elements")
        if not isinstance(elements, list):
            return []

        target_terms = self._selector_search_terms(raw_selector, text_hint, intent)
        if not target_terms:
            return []

        duplicate_counts: dict[str, int] = {}
        duplicate_id_counts: dict[str, int] = {}
        for item in elements:
            if not isinstance(item, dict):
                continue
            label = self._snapshot_item_label(item)
            if label:
                duplicate_counts[label] = duplicate_counts.get(label, 0) + 1
            item_id = str(item.get("id", "")).strip()
            if item_id:
                duplicate_id_counts[item_id] = duplicate_id_counts.get(item_id, 0) + 1

        ranked: list[tuple[int, int, list[str]]] = []
        for order, item in enumerate(elements):
            if not isinstance(item, dict):
                continue
            if not self._snapshot_item_matches_intent(item, intent):
                continue
            score = self._snapshot_match_score(item, target_terms, step_type, intent, duplicate_counts)
            if score <= 0:
                continue
            selectors = item.get("selectors")
            normalized: list[str] = []
            if isinstance(selectors, list):
                normalized.extend(
                    str(selector).strip()
                    for selector in selectors
                    if str(selector).strip()
                    and not self._selector_uses_duplicate_id(str(selector), duplicate_id_counts)
                )
            normalized.extend(self._selectors_from_snapshot_item(item, step_type, duplicate_id_counts))
            normalized = self._dedupe(normalized)
            if normalized:
                ranked.append((score, order, normalized))

        if intent.element_type == "listitem" and intent.ordinal is not None and ranked:
            ordinal_ranked = sorted(ranked, key=lambda entry: entry[1])
            ordinal_index = max(intent.ordinal - 1, 0)
            if ordinal_index < len(ordinal_ranked):
                selected = ordinal_ranked[ordinal_index][2]
                return self._rank_live_snapshot_selectors(self._dedupe(selected), intent)

        ranked.sort(key=lambda entry: (-entry[0], entry[1]))
        flattened: list[str] = []
        for _, _, selectors in ranked[:8]:
            flattened.extend(selectors)
        return self._rank_live_snapshot_selectors(self._dedupe(flattened), intent)

    def _preferred_scopes_for_intent(self, intent: StepIntent) -> list[str]:
        if intent.scope_hint:
            return [intent.scope_hint, "main", "form", "article", "body"]
        if intent.element_type == "input":
            return ["form", "search", "main", "header", "body"]
        if intent.element_type == "button":
            return ["form", "main", "article", "dialog", "body"]
        if intent.element_type == "link":
            target = (intent.target_text or "").lower()
            if any(token in target for token in ("home", "settings", "profile", "logout", "sign out", "sign in")):
                return ["nav", "header", "main", "body"]
            return ["main", "article", "nav", "body"]
        if intent.element_type == "listitem":
            return ["main", "article", "body"]
        return ["main", "form", "article", "body"]

    def _scope_score(self, item_scope: str, intent: StepIntent) -> int:
        scope = (item_scope or "body").strip().lower()
        if not scope:
            scope = "body"
        preferred = self._preferred_scopes_for_intent(intent)
        if scope in preferred:
            rank = preferred.index(scope)
            return max(18 - rank * 4, 4)
        if scope in {"nav", "aside", "footer"} and intent.element_type in {"input", "button", "listitem"}:
            return -8
        if scope == "header" and intent.element_type == "listitem":
            return -6
        return 0

    def _snapshot_item_matches_intent(self, item: dict[str, Any], intent: StepIntent) -> bool:
        tag = str(item.get("tag", "")).lower()
        role = str(item.get("role", "")).lower()
        scope = str(item.get("scope", "")).lower()
        visible = bool(item.get("visible", True))
        haystack = " ".join(
            str(item.get(field, "")).lower()
            for field in ("text", "aria", "name", "id", "testid", "placeholder", "title", "href")
        )
        if not visible and intent.element_type in {"input", "button", "link", "listitem"}:
            return False
        preferred_scopes = self._preferred_scopes_for_intent(intent)
        if scope and scope not in preferred_scopes and scope in {"nav", "aside", "footer"}:
            if intent.element_type in {"input", "button", "listitem"}:
                return False
        if intent.element_type == "input":
            return tag in {"input", "textarea", "select"} or role in {"textbox", "searchbox", "combobox"}
        if intent.element_type == "button":
            return tag in {"button", "input"} or role in {"button", "menuitem", "tab"}
        if intent.element_type == "link":
            return tag == "a" or role == "link"
        if intent.element_type == "listitem":
            return (
                tag in {"a", "li", "article", "div"}
                or role in {"listitem", "article", "link"}
                or any(token in haystack for token in ("result", "product", "video", "title"))
            )
        return True

    def _selector_search_terms(
        self,
        raw_selector: str,
        text_hint: str | None,
        intent: StepIntent | None = None,
    ) -> list[str]:
        tokens: list[str] = []
        for source in (
            raw_selector,
            text_hint or "",
            self._extract_selector_text(raw_selector) or "",
            (intent.target_text if intent else "") or "",
        ):
            lowered = source.strip().lower()
            if not lowered:
                continue
            lowered = lowered.replace("{{selector.", " ").replace("}}", " ").replace("_", " ").replace("-", " ")
            parts = re.findall(r"[a-z0-9]+", lowered)
            for part in parts:
                if len(part) >= 3 and part not in {
                    "selector",
                    "input",
                    "button",
                    "click",
                    "type",
                    "wait",
                    "text",
                    "result",
                    "results",
                    "products",
                    "link",
                    "main",
                    "list",
                    "data",
                    "component",
                    "renderer",
                    "query",
                    "first",
                    "second",
                    "third",
                    "type",
                    "videoid",
                }:
                    tokens.append(part)
        return self._dedupe(tokens)

    @staticmethod
    def _snapshot_item_label(item: dict[str, Any]) -> str:
        for field in ("text", "aria", "title", "name", "placeholder"):
            value = str(item.get(field, "")).strip().lower()
            if value:
                return value[:120]
        return ""

    @staticmethod
    def _selector_uses_duplicate_id(selector: str, duplicate_id_counts: dict[str, int] | None) -> bool:
        value = selector.strip()
        if not value.startswith("#"):
            return False
        selector_id = value[1:]
        return int((duplicate_id_counts or {}).get(selector_id, 0)) > 1

    def _snapshot_match_score(
        self,
        item: dict[str, Any],
        target_terms: list[str],
        step_type: str,
        intent: StepIntent | None = None,
        duplicate_counts: dict[str, int] | None = None,
    ) -> int:
        haystack_parts = [
            str(item.get("tag", "")),
            str(item.get("type", "")),
            str(item.get("text", "")),
            str(item.get("aria", "")),
            str(item.get("name", "")),
            str(item.get("id", "")),
            str(item.get("testid", "")),
            str(item.get("role", "")),
            str(item.get("placeholder", "")),
            str(item.get("href", "")),
            str(item.get("title", "")),
        ]
        haystack = " ".join(part.lower() for part in haystack_parts if part).strip()
        if not haystack:
            return 0

        score = 0
        keyword_matches = 0
        label = self._snapshot_item_label(item)
        duplicate_count = 0
        if duplicate_counts and label:
            duplicate_count = int(duplicate_counts.get(label, 0))
        for term in target_terms:
            if term in haystack:
                keyword_matches += 1
                score += max(8, len(term))

        if target_terms and keyword_matches == 0:
            return 0

        tag = str(item.get("tag", "")).lower()
        role = str(item.get("role", "")).lower()
        if intent is not None:
            score += self._scope_score(str(item.get("scope", "")), intent)
            target_text = (intent.target_text or "").strip().lower()
            if target_text and label:
                if target_text == label:
                    score += 24
                elif target_text in label:
                    score += 12
            stable_attrs = sum(
                1
                for field in ("id", "testid", "name")
                if str(item.get(field, "")).strip()
            )
            score += min(stable_attrs * 5, 12)
            if duplicate_count > 1:
                score -= min((duplicate_count - 1) * 5, 12)
        if bool(item.get("visible", True)):
            score += 12
        else:
            score -= 20
        if bool(item.get("enabled", True)):
            score += 6
        else:
            score -= 10
        if step_type in {"type", "select"}:
            if tag in {"input", "textarea", "select"}:
                score += 12
            if role in {"textbox", "combobox"}:
                score += 8
        elif step_type in {"click", "handle_popup"}:
            if tag in {"button", "a"}:
                score += 12
            if role in {"button", "link", "menuitem", "tab", "combobox"}:
                score += 8
        elif step_type in {"wait", "verify_text"}:
            score += 4

        language_intent = any(term in {"language", "locale", "lang"} for term in target_terms)
        if language_intent:
            text_value = str(item.get("text", "")).strip()
            uppercase_code = len(text_value) <= 5 and text_value.isupper()
            if any(token in haystack for token in ("language", "locale", "lang")):
                score += 20
            if tag in {"button", "select"}:
                score += 10
            if role in {"button", "combobox", "menuitem"}:
                score += 10
            if uppercase_code:
                score += 14

        if intent is not None:
            target_text = (intent.target_text or "").lower()
            if target_text and not any(token in target_text for token in ("clear", "close", "dismiss", "remove")):
                if any(token in haystack for token in ("clear", "close", "dismiss", "remove", "reset query")):
                    score -= 18

        return score

    def _selectors_from_snapshot_item(
        self,
        item: dict[str, Any],
        step_type: str,
        duplicate_id_counts: dict[str, int] | None = None,
    ) -> list[str]:
        selectors: list[str] = []
        tag = str(item.get("tag", "")).strip().lower()
        role = str(item.get("role", "")).strip()
        item_id = str(item.get("id", "")).strip()
        name = str(item.get("name", "")).strip()
        testid = str(item.get("testid", "")).strip()
        aria = str(item.get("aria", "")).strip()
        placeholder = str(item.get("placeholder", "")).strip()
        text = str(item.get("text", "")).strip()
        title = str(item.get("title", "")).strip()

        if item_id and int((duplicate_id_counts or {}).get(item_id, 0)) <= 1:
            selectors.append(f"#{item_id}")
        if testid:
            selectors.append(f'[data-testid="{self._escape_playwright_text(testid)}"]')
        if name and tag in {"input", "textarea", "select"}:
            selectors.append(f'{tag}[name="{self._escape_playwright_text(name)}"]')
        if aria:
            escaped_aria = self._escape_playwright_text(aria[:60])
            if tag:
                selectors.append(f'{tag}[aria-label*="{escaped_aria}"]')
            if role:
                selectors.append(f'[role="{self._escape_playwright_text(role)}"][aria-label*="{escaped_aria}"]')
            selectors.append(f'[aria-label*="{escaped_aria}"]')
        if title:
            selectors.append(f'[title*="{self._escape_playwright_text(title[:60])}"]')
        if placeholder and tag in {"input", "textarea"}:
            selectors.append(f'{tag}[placeholder*="{self._escape_playwright_text(placeholder[:60])}"]')
        if text:
            escaped_text = self._escape_playwright_text(text[:80])
            if step_type in {"click", "handle_popup"}:
                if tag == "button":
                    selectors.append(f'button:has-text("{escaped_text}")')
                elif tag == "select":
                    selectors.append(f'select:has-text("{escaped_text}")')
                elif tag == "a":
                    selectors.append(f'a:has-text("{escaped_text}")')
                elif role:
                    selectors.append(f'[role="{self._escape_playwright_text(role)}"]:has-text("{escaped_text}")')
            selectors.append(f"text={text[:80]}")

        return self._dedupe(selectors)

    @staticmethod
    def _selector_stability_score(selector: str) -> int:
        lowered = selector.strip().lower()
        if not lowered:
            return -100

        score = 0
        if lowered.startswith("#"):
            score += 36
        if "[data-testid=" in lowered:
            score += 32
        if "name=" in lowered:
            score += 20
        if "[aria-label" in lowered or "[title" in lowered or "[placeholder" in lowered:
            score += 12
        if lowered.startswith("text="):
            score -= 18
        if ":has-text(" in lowered:
            score -= 10
        if any(token in lowered for token in (":nth-of-type(", ":first-", "nth=")):
            score -= 24
        if " >> " in lowered:
            score -= 10
        return score

    def _live_selector_score(self, selector: str, intent: StepIntent) -> int:
        score = self._intent_selector_score(selector, intent)
        if intent.element_type == "listitem" and intent.ordinal is not None:
            return score
        return score + self._selector_stability_score(selector)

    async def _probe_element_present(self, selector: str, timeout_ms: int = 1500) -> bool:
        """
        Fast-fail DOM probe: returns True if the selector resolves to at least
        one visible element, False otherwise.

        Uses an immediate is_visible() check on ALL matches (not just DOM-first)
        to avoid false failures caused by a hidden element appearing before a
        visible one in DOM order.  Falls back to a short wait_for_selector if
        none are immediately visible.
        """
        browser = getattr(self, "_browser", None)
        if browser is None:
            return True

        # Try immediate is_visible() check on each matching locator first.
        # This avoids waiting for a hidden first-match when a later match is
        # already visible (common when nav and form both have the same button text).
        try:
            page = browser._active_context().page  # type: ignore[attr-defined]
            locators = page.locator(selector)
            count = await locators.count()
            for i in range(min(count, 6)):
                try:
                    if await locators.nth(i).is_visible():
                        return True
                except Exception:
                    continue
            # If none are immediately visible, give a short wait for slow renders.
            if count > 0:
                try:
                    await asyncio.wait_for(
                        page.wait_for_selector(selector, state="visible", timeout=min(timeout_ms, 1500)),
                        timeout=min(timeout_ms, 1500) / 1000 + 0.5,
                    )
                    return True
                except Exception:
                    return False
        except AttributeError:
            pass

        # Fallback: original wait_for path (for non-Playwright browser clients).
        if not hasattr(browser, "wait_for"):
            return True
        try:
            await asyncio.wait_for(
                browser.wait_for(
                    until="selector_visible",
                    ms=timeout_ms,
                    selector=selector,
                    load_state=None,
                ),
                timeout=timeout_ms / 1000 + 0.5,
            )
            return True
        except Exception:
            return False

    def _candidate_timeout_seconds(self, candidate_count: int, step_type: str | None = None) -> float:
        step_timeout = max(float(self._settings.step_timeout_seconds), 1.0)
        if candidate_count <= 1:
            if step_type == "type":
                return min(step_timeout, 2.5)
            if step_type == "click":
                return min(step_timeout, 3.0)
            if step_type == "select":
                return min(step_timeout, 3.0)
            if step_type == "wait":
                return min(step_timeout, 3.0)
            return step_timeout
        budget = max(step_timeout - 0.5, 1.0)
        per_candidate = budget / candidate_count
        if step_type == "type":
            per_candidate = min(per_candidate, 3.0)
        if step_type == "click":
            per_candidate = min(per_candidate, 4.0)
        if step_type == "select":
            per_candidate = min(per_candidate, 3.0)
        if step_type == "wait":
            per_candidate = min(per_candidate, 4.0)
        return max(min(per_candidate, step_timeout), 1.0)

    def _intent_selector_score(self, selector: str, intent: StepIntent) -> int:
        lowered = selector.strip().lower()
        if not lowered:
            return -100

        score = 0
        is_link_like = any(token in lowered for token in ("a:", "a[", "href", "role=\"link\"", "role='link'"))
        is_button_like = any(token in lowered for token in ("button", "role=\"button\"", "role='button'", "submit", "menuitem", "tab"))
        if intent.element_type == "input":
            if any(token in lowered for token in ("input", "textarea", "select", "textbox", "searchbox", "combobox", "placeholder", "name=")):
                score += 30
            if any(token in lowered for token in ("button", "a:", "href", "role=\"link\"", "role='link'")):
                score -= 10
        elif intent.element_type == "button":
            if is_button_like:
                score += 30
            if any(token in lowered for token in ("input[type='text']", "textarea", "searchbox", "textbox")):
                score -= 12
        elif intent.element_type == "link":
            if any(token in lowered for token in (" a", "a:", "a[", "href", "role=\"link\"", "role='link'", "h2 a", "title link")):
                score += 30
            if "button" in lowered and "a[role='button']" not in lowered and 'a[role="button"]' not in lowered:
                score -= 10
        elif intent.element_type == "listitem":
            if any(token in lowered for token in ("result", "product", "video", "data-asin", "s-search-result", "listitem", "article", "title-recipe-title", "h2 a")):
                score += 28
            if ".a-link-normal" in lowered and "h2 a" not in lowered and "title-recipe-title" not in lowered:
                score -= 8
            if (
                intent.action == "click"
                and lowered.startswith("div")
                and not any(
                    token in lowered
                    for token in (" h2 a", "a[", "a:", " a ", "button", "role=\"button\"", "role='button'", "title-recipe-title")
                )
            ):
                score -= 24

        target_text = (intent.target_text or "").strip().lower()
        if target_text:
            if target_text in lowered:
                score += 20
            else:
                for term in re.findall(r"[a-z0-9]+", target_text):
                    if len(term) >= 3 and term in lowered:
                        score += 6
            if not any(token in target_text for token in ("clear", "close", "dismiss", "remove")):
                if any(token in lowered for token in ("clear", "close", "dismiss", "remove", "reset query")):
                    score -= 20
            salient_terms = self._salient_click_target_terms(target_text)
            if intent.action == "click" and salient_terms:
                matched_salient_terms = sum(
                    1 for term in salient_terms if self._matches_click_target_term(lowered, term)
                )
                if is_button_like and matched_salient_terms:
                    score += 10 + (matched_salient_terms * 4)
                if intent.element_type != "link" and is_link_like:
                    if matched_salient_terms == 0:
                        score -= 18
                    elif matched_salient_terms < len(salient_terms):
                        score -= 8

        if intent.scope_hint and intent.scope_hint in lowered:
            score += 8

        if intent.ordinal is not None:
            ordinal = intent.ordinal
            if f">> nth={max(ordinal - 1, 0)}" in lowered:
                score += 16
            if f"nth-of-type({ordinal})" in lowered:
                score += 16
            if f"nth={max(ordinal - 1, 0)}" in lowered:
                score += 12
            other_ordinals = {1, 2, 3, 4, 5} - {ordinal}
            if any(f"nth-of-type({other})" in lowered for other in other_ordinals):
                score -= 18

        return score

    def _rank_live_snapshot_selectors(self, selectors: list[str], intent: StepIntent) -> list[str]:
        if not selectors:
            return selectors
        scored = [
            (
                self._live_selector_score(selector, intent),
                self._selector_stability_score(selector),
                index,
                selector,
            )
            for index, selector in enumerate(selectors)
        ]
        scored.sort(key=lambda entry: (-entry[0], -entry[1], entry[2]))
        return [selector for _, _, _, selector in scored]

    def _rank_selectors_by_intent(self, selectors: list[str], intent: StepIntent) -> list[str]:
        if not selectors:
            return selectors
        scored = [
            (self._intent_selector_score(selector, intent), index, selector)
            for index, selector in enumerate(selectors)
        ]
        scored.sort(key=lambda entry: (-entry[0], entry[1]))
        return [selector for _, _, selector in scored]

    def _scored_selectors_by_intent(self, selectors: list[str], intent: StepIntent) -> list[tuple[int, str]]:
        if not selectors:
            return []
        scored = [
            (self._intent_selector_score(selector, intent), selector)
            for selector in selectors
        ]
        scored.sort(key=lambda entry: entry[0], reverse=True)
        return scored

    def _confidence_gate_candidates(
        self,
        selectors: list[str],
        intent: StepIntent,
        *,
        step_type: str,
        source: str,
    ) -> tuple[list[str], CandidateConfidence]:
        if source == "live":
            scored = [
                (
                    self._live_selector_score(selector, intent),
                    index,
                    selector,
                )
                for index, selector in enumerate(selectors)
            ]
            scored.sort(key=lambda entry: (-entry[0], entry[1]))
            scored = [(score, selector) for score, _, selector in scored]
        else:
            scored = self._scored_selectors_by_intent(selectors, intent)
        if not scored:
            return [], CandidateConfidence(
                level="none",
                top_score=0,
                second_score=None,
                score_gap=0,
                retained_count=0,
            )

        top_score = scored[0][0]
        second_score = scored[1][0] if len(scored) > 1 else None
        score_gap = top_score - second_score if second_score is not None else top_score

        if source == "initial":
            if top_score >= 42 and score_gap >= 10:
                level = "high"
            elif top_score >= 26 and score_gap >= 4:
                level = "medium"
            else:
                level = "low"
        else:
            if top_score >= 34 and score_gap >= 8:
                level = "high"
            elif top_score >= 20 and score_gap >= 3:
                level = "medium"
            else:
                level = "low"

        narrowed = self._narrow_candidates_by_confidence(
            scored,
            intent,
            step_type=step_type,
            level=level,
            source=source,
        )
        return narrowed, CandidateConfidence(
            level=level,
            top_score=top_score,
            second_score=second_score,
            score_gap=score_gap,
            retained_count=len(narrowed),
        )

    def _narrow_candidates_by_confidence(
        self,
        scored: list[tuple[int, str]],
        intent: StepIntent,
        *,
        step_type: str,
        level: str,
        source: str,
    ) -> list[str]:
        if not scored:
            return []

        if source == "initial" and step_type in {"type", "select"}:
            return [selector for _, selector in scored]

        if level == "high":
            if step_type in {"type", "select"}:
                limit = 6
            elif step_type == "verify_text":
                limit = 2
            else:
                limit = 3
            retained = [
                selector
                for score, selector in scored
                if score >= 0 and self._selector_preserves_intent(selector, intent, source=source)
            ][:limit]
            if retained:
                return retained
            return [selector for _, selector in scored[:limit]]

        if level == "medium":
            top_score = scored[0][0]
            floor = max(top_score - 8, 10)
            limit = 8 if step_type in {"type", "select"} else 5
            retained = [selector for score, selector in scored if score >= floor][:limit]
            if retained:
                return retained

        strict = [
            selector
            for score, selector in scored
            if score >= 0 and self._selector_preserves_intent(selector, intent, source=source)
        ]
        if strict:
            return strict[:4]
        return [selector for _, selector in scored[:3]]

    def _selector_preserves_intent(self, selector: str, intent: StepIntent, *, source: str) -> bool:
        lowered = selector.lower()
        if intent.ordinal is not None:
            preserves_ordinal = (
                f"nth-of-type({intent.ordinal})" in lowered
                or f">> nth={max(intent.ordinal - 1, 0)}" in lowered
                or f"nth={max(intent.ordinal - 1, 0)}" in lowered
            )
            if not preserves_ordinal:
                return False
        if intent.element_type == "input":
            return any(token in lowered for token in ("input", "textarea", "select", "textbox", "searchbox", "combobox"))
        if intent.element_type == "button":
            return any(token in lowered for token in ("button", "role=\"button\"", "role='button'", "submit"))
        if intent.element_type == "link":
            return any(token in lowered for token in (" a", "a:", "a[", "href", "role=\"link\"", "role='link'"))
        if intent.element_type == "listitem":
            if ".a-link-normal" in lowered and "h2 a" not in lowered and "title-recipe-title" not in lowered:
                return False
            if source == "live":
                return any(token in lowered for token in ("h2 a", "title-recipe-title", "listitem", "article", "s-search-result"))
            return any(token in lowered for token in ("h2 a", "title-recipe-title", "s-search-result", "result", "product", "video"))
        return True

    def _candidate_execution_policy(self, confidence: CandidateConfidence, *, step_type: str) -> dict[str, Any]:
        if confidence.level == "high":
            return {
                "mode": "direct",
                "recovery_attempts": 1,
                "allow_live_candidates": False,
                "allow_llm_recovery": False,
            }
        if confidence.level == "medium":
            return {
                "mode": "validated",
                "recovery_attempts": 1,
                "allow_live_candidates": step_type in {"click", "wait", "verify_text"},
                "allow_llm_recovery": False,
            }
        return {
            "mode": "recovering",
            "recovery_attempts": None,
            "allow_live_candidates": True,
            "allow_llm_recovery": True,
        }

    def _llm_recovery_allowed_for_active_step(self) -> bool:
        trace = self._active_step_trace()
        if not trace:
            return True
        groups = trace.get("attempt_groups")
        if not isinstance(groups, list):
            return True
        for group in reversed(groups):
            if not isinstance(group, dict):
                continue
            if group.get("kind") != "selector_fallback":
                continue
            policy = group.get("execution_policy")
            if isinstance(policy, dict):
                allowed = policy.get("allow_llm_recovery")
                if isinstance(allowed, bool):
                    return allowed
            confidence = group.get("candidate_confidence")
            if isinstance(confidence, dict) and confidence.get("level") in {"high", "medium"}:
                return False
            break
        return True

    def _active_step_has_llm_recovery_group(self) -> bool:
        trace = self._active_step_trace()
        if not trace:
            return False
        groups = trace.get("attempt_groups")
        if not isinstance(groups, list):
            return False
        return any(isinstance(group, dict) and group.get("kind") == "llm_selector_recovery" for group in groups)

    def _intent_label_selector_candidates(
        self,
        step_type: str,
        text_hint: str | None,
        raw_selector: str,
    ) -> list[str]:
        candidates: list[str] = []
        selector_text = raw_selector.strip()
        if not selector_text:
            return []
        if selector_text.startswith(("#", ".", "[", "/", "xpath=", "text=", "input", "button", "select", "textarea", "a:", "a[", "div", "span")):
            return []

        # For dynamic label:has-text('Field Name') selectors, extract the label text
        # so that meaningful keywords like "organization" or "date" are used rather
        # than the noise words "label", "has", "text".
        _label_text_match = re.search(r"label:has-text\(['\"](.+?)['\"]\)", selector_text, re.IGNORECASE)
        if _label_text_match:
            _base = _label_text_match.group(1)
            combined = " ".join(part for part in (_base, text_hint or "") if part).lower()
        else:
            combined = " ".join(
                part for part in (raw_selector, text_hint or "") if part
            ).lower()
            combined = combined.replace("{{selector.", "").replace("}}", "")
        combined = combined.replace("_", " ").replace("-", " ")
        keywords = [
            word for word in re.findall(r"[a-z0-9]+", combined)
            if len(word) >= 3 and word not in {
                "selector", "input", "button", "click", "type",
                "form", "the", "and", "for"
            }
        ]

        if not keywords:
            return []

        for keyword in keywords[:3]:
            if step_type in {"type", "select"}:
                candidates.extend([
                    f"input[placeholder*='{keyword}']",
                    f"input[aria-label*='{keyword}']",
                    f"textarea[placeholder*='{keyword}']",
                    f"input[name*='{keyword}']",
                    f"[data-testid*='{keyword}'] input",
                    f"label:has-text('{keyword}') + input",
                    f"label:has-text('{keyword}') ~ input",
                ])
            elif step_type == "click":
                candidates.extend([
                    f"button:has-text('{keyword}')",
                    f"[role='button']:has-text('{keyword}')",
                    f"a:has-text('{keyword}')",
                    f"[aria-label*='{keyword}']",
                    f"[data-testid*='{keyword}']",
                ])
            elif step_type == "verify_text":
                candidates.extend([
                    f"text={keyword}",
                    f"[role='alert']:has-text('{keyword}')",
                    f"[role='status']:has-text('{keyword}')",
                    f".toast:has-text('{keyword}')",
                ])

        return self._dedupe(candidates)

    def _get_profile_candidates_only(
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
        intent = self._build_step_intent(step_type, selector, text_hint)

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
            if "workflowname" in selector_lower or "workflow_name" in selector_lower or "workflow name" in selector_lower:
                keys.insert(0, "workflow_name")
            if "workflow_description" in selector_lower or "description" in selector_lower:
                keys.insert(0, "workflow_description")
            if "status_name" in selector_lower or "status name" in selector_lower or "statusname" in selector_lower:
                keys.insert(0, "status_name")
            if "label" in selector_lower:
                keys.insert(0, "form_label")
            if "dropdown_option_label" in selector_lower:
                keys.insert(0, "dropdown_option_label")
            if "dropdown_option_value" in selector_lower:
                keys.insert(0, "dropdown_option_value")
            if any(token in selector_lower for token in ("twotabsearchtextbox", "field-keywords")):
                keys.insert(0, "amazon_search_box")
        amazon_result_position = self._amazon_result_position(selector_lower, text_hint) if step_type == "click" else None
        youtube_result_position = self._youtube_result_position(selector_lower, text_hint) if step_type == "click" else None
        if step_type == "click":
            if any(token in selector_lower for token in ("login", "sign in", "signin", "log in")):
                keys.insert(0, "login_button")
            if any(token in selector_lower for token in ("language", "locale", "change language", "change locale", "lang")):
                keys.insert(0, "language_switcher")
            if "create_form_confirm" in selector_lower or "create form confirm" in selector_lower:
                keys.insert(0, "create_form_confirm")
            elif "create form" in selector_lower or "create_form" in selector_lower or "createform" in selector_lower:
                keys.insert(0, "create_form")
            if "create workflow" in selector_lower or "create_workflow" in selector_lower or "createworkflow" in selector_lower:
                keys.insert(0, "create_workflow")
            if "add status" in selector_lower or "add_status" in selector_lower or "addstatus" in selector_lower:
                keys.insert(0, "add_status_button")
            if "new status" in selector_lower or "new_status" in selector_lower or "newstatus" in selector_lower:
                keys.insert(0, "new_status_tab")
            if "from_status_dropdown" in selector_lower or "from status" in selector_lower:
                keys.insert(0, "from_status_dropdown")
            if "to_status_dropdown" in selector_lower or "to status" in selector_lower:
                keys.insert(0, "to_status_dropdown")
            if "transition_canvas_label" in selector_lower:
                keys.insert(0, "transition_canvas_label")
            if "workflow_list_item" in selector_lower or "qa_auto_workflow_" in selector_lower:
                keys.insert(0, "workflow_list_item")
            if "top_left_corner" in selector_lower or "top left corner" in selector_lower:
                keys.insert(0, "top_left_corner")
            if "workflows_module" in selector_lower or selector_lower.strip() == "workflows":
                keys.insert(0, "workflows_module")
            if "form_list_first_name" in selector_lower:
                keys.insert(0, "form_list_first_name")
            if "back button" in selector_lower or "selector.back_button" in selector_lower or selector_lower.strip() == "back":
                keys.insert(0, "back_button")
            if "save workflow" in selector_lower or "save_workflow" in selector_lower or "saveworkflow" in selector_lower:
                keys.insert(0, "save_workflow")
            if "save status" in selector_lower or "save_status" in selector_lower or "savestatus" in selector_lower:
                keys.insert(0, "save_status")
            if "status_category_todo" in selector_lower or "to do" == selector_lower.strip():
                keys.insert(0, "status_category_todo")
            if "status_category_dropdown" in selector_lower or "select category" in selector_lower or "status category" in selector_lower:
                keys.insert(0, "status_category_dropdown")
            if "save form" in selector_lower or "save_form" in selector_lower or "saveform" in selector_lower:
                keys.insert(0, "save_form")
        if step_type == "click" and text_hint and self._looks_like_transition_hint(text_hint):
            candidates_from_hint = self._transition_label_signal_selectors(text_hint, test_data)
        else:
            candidates_from_hint = []
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
            if amazon_result_position is not None:
                if amazon_result_position == 2:
                    keys.insert(0, "amazon_second_result")
                else:
                    candidates_from_hint.extend(self._amazon_result_candidates(amazon_result_position))
            if youtube_result_position is not None:
                candidates_from_hint.extend(self._youtube_result_candidates(youtube_result_position))
            elif any(
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
            if any(token in hint_lower for token in ("create workflow", "create_workflow", "createworkflow")):
                keys.insert(0, "create_workflow")
            if any(token in hint_lower for token in ("workflow has been created",)):
                keys.insert(0, "workflow_confirmation")
            if any(token in hint_lower for token in ("login", "sign in", "signin", "log in")):
                keys.insert(0, "login_button")
            if any(token in hint_lower for token in ("save", "save form", "save_form")):
                keys.insert(0, "save_form")
            if any(token in hint_lower for token in ("save workflow", "save_workflow")):
                keys.insert(0, "save_workflow")
            if any(token in hint_lower for token in ("save status", "save_status")):
                keys.insert(0, "save_status")
            if "create form" in selector_lower or "create_form" in selector_lower or "createform" in selector_lower:
                keys.insert(0, "create_form")
            if "create workflow" in selector_lower or "create_workflow" in selector_lower or "createworkflow" in selector_lower:
                keys.insert(0, "create_workflow")
            if "workflow_confirmation" in selector_lower or "workflow has been created" in selector_lower:
                keys.insert(0, "workflow_confirmation")
            if "form_list_first_row" in selector_lower:
                keys.insert(0, "form_list_first_row")
        if step_type == "wait":
            if "dropdown_options_section" in selector_lower:
                keys.insert(0, "dropdown_options_section")
            if "create_workflow" in selector_lower or "create workflow" in selector_lower:
                keys.insert(0, "create_workflow")

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
            # Prefer remembered selectors that already succeeded on this domain,
            # except for brittle dropdown modal actions where profile selectors are safer.
            if key not in strict_dropdown_keys and step_type != "type":
                candidates.extend(self._memory_candidates(run_domain, step_type, key))
            profile_candidates = self._merge_profile_candidates(key, selector_profile, run_domain=run_domain)
            for candidate in profile_candidates:
                normalized = self._apply_template(candidate, test_data).strip()
                if normalized:
                    candidates.append(normalized)

        if not alias_key:
            if step_type != "type":
                candidates.extend(self._memory_candidates(run_domain, step_type, selector))
            candidates.append(selector)
            candidates.extend(self._derive_selector_variants(selector, step_type))

        if step_type == "type":
            if alias_key:
                deduped = self._dedupe(candidates)
            else:
                deduped = self._dedupe([selector] + candidates)
        else:
            deduped = self._dedupe(candidates)
        if candidates_from_hint:
            deduped = self._dedupe(candidates_from_hint + deduped)
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
        if (
            step_type == "click"
            and (intent.target_text or "").strip().lower() == "search"
            and intent.element_type == "button"
        ):
            deduped = [
                candidate
                for candidate in deduped
                if "clear search query" not in candidate.lower()
                and "reset query" not in candidate.lower()
            ]
        if step_type == "click" and amazon_result_position is not None and amazon_result_position > 1:
            deduped = [
                candidate
                for candidate in deduped
                if "h2 a.a-link-normal" not in candidate.lower() and candidate.lower().strip() != "h2 a"
            ]
        return self._rank_selectors_by_intent(deduped, intent)

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
        intent = self._build_step_intent(step_type, selector, text_hint)

        intent_candidates = self._intent_label_selector_candidates(
            step_type, text_hint, selector
        )

        return self._dedupe(self._get_profile_candidates_only(
            raw_selector,
            step_type,
            selector_profile,
            test_data,
            run_domain,
            text_hint,
        ) + intent_candidates)

    @staticmethod
    def _amazon_result_position(selector_lower: str, text_hint: str | None = None) -> int | None:
        signal = " ".join(part for part in (selector_lower, (text_hint or "").lower()) if part).strip()
        if not any(token in signal for token in ("amazon", "s-search-result", "product", "results")):
            widget_match = re.search(r"search-results[_-](\d+)", signal)
            if widget_match:
                try:
                    return max(int(widget_match.group(1)), 1)
                except Exception:
                    return None
            return None
        nth_of_type_match = re.search(r"nth-of-type\((\d+)\)", signal)
        if nth_of_type_match:
            return max(int(nth_of_type_match.group(1)), 1)
        nth_child_match = re.search(r"nth-child\((\d+)\)", signal)
        if nth_child_match:
            return max(int(nth_child_match.group(1)), 1)
        widget_match = re.search(r"search-results[_-](\d+)", signal)
        if widget_match:
            try:
                return max(int(widget_match.group(1)), 1)
            except Exception:
                return None
        for word, value in (
            ("first", 1),
            ("second", 2),
            ("third", 3),
            ("fourth", 4),
            ("fifth", 5),
        ):
            if word in signal:
                return value
        return None

    @staticmethod
    def _amazon_result_candidates(position: int) -> list[str]:
        index = max(position - 1, 0)
        nth = max(position, 1)
        return [
            f"div[data-component-type='s-search-result'] h2 a >> nth={index}",
            f"div[data-component-type='s-search-result'] [data-cy='title-recipe-title'] a >> nth={index}",
            f"div[data-component-type='s-search-result']:nth-of-type({nth}) h2 a",
            f"div[data-component-type='s-search-result']:nth-of-type({nth}) [data-cy='title-recipe-title'] a",
        ]

    @staticmethod
    def _youtube_result_position(selector_lower: str, text_hint: str | None = None) -> int | None:
        signal = " ".join(part for part in (selector_lower, (text_hint or "").lower()) if part).strip()
        if not any(token in signal for token in ("youtube", "video-title", "ytd-video-renderer", "video result", "video")):
            return None
        nth_match = re.search(r">>\s*nth=(\d+)", signal)
        if nth_match:
            return int(nth_match.group(1)) + 1
        nth_of_type_match = re.search(r"nth-of-type\((\d+)\)", signal)
        if nth_of_type_match:
            return max(int(nth_of_type_match.group(1)), 1)
        if ":first-of-type" in signal:
            return 1
        for word, value in (
            ("first", 1),
            ("second", 2),
            ("third", 3),
            ("fourth", 4),
            ("fifth", 5),
        ):
            if word in signal:
                return value
        return None

    @staticmethod
    def _youtube_result_candidates(position: int) -> list[str]:
        index = max(position - 1, 0)
        nth = max(position, 1)
        return [
            f"a#video-title >> nth={index}",
            f"ytd-video-renderer a#video-title >> nth={index}",
            f"ytd-video-renderer:nth-of-type({nth}) a#video-title",
        ]

    def _initialize_runtime_test_data(self, test_data: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(test_data)
        stable_now = datetime.now()
        normalized.setdefault("NOW", stable_now.strftime("%Y-%m-%d_%H-%M-%S"))
        normalized.setdefault("TIMESTAMP", normalized["NOW"])
        normalized.setdefault("CURRENT_TIMESTAMP", normalized["NOW"])
        normalized.setdefault("NOW_YYYYMMDD_HHMMSS", stable_now.strftime("%Y%m%d_%H%M%S"))
        normalized.setdefault("NOW_YYYYMMDDHHMMSS", stable_now.strftime("%Y%m%d%H%M%S"))
        return normalized

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

        if key == "status_category_todo":
            filtered = [
                c for c in candidates
                if "to do" in c.lower() and "<option" not in c.lower() and "text=to do" not in c.lower()
            ]
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

        if key == "form_name":
            preferred_tokens = (
                "div[role='dialog'] input#name",
                "div[role='dialog'] input[name='name']",
                "div[role='dialog'] input[placeholder='enter a name']",
                "div[role='dialog'] input[placeholder*='enter a name']",
                "div[role='dialog'] input[data-slot='input'][name='name']",
                "input#name",
                "input[name='name']",
                "placeholder='enter a name'",
                "placeholder*='enter a name'",
                "data-slot='input'",
                "placeholder*='name'",
            )
            blocked_tokens = (
                "search",
                "email",
                "password",
                "username",
                "login",
                "signin",
                "sign in",
                "phone",
                "tel",
            )
            filtered = [
                candidate
                for candidate in candidates
                if any(token in candidate.lower() for token in preferred_tokens)
                and not any(token in candidate.lower() for token in blocked_tokens)
            ]
            return filtered or candidates

        if key == "workflow_name":
            preferred_tokens = (
                "input[name='workflowname']",
                "input#workflowname",
                "input#workflow-name",
                "placeholder*='workflow name'",
            )
            filtered = [
                candidate
                for candidate in candidates
                if any(token in candidate.lower() for token in preferred_tokens)
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
                "placeholder*='email'",
                "placeholder*=\"email\"",
                "aria-label*='email'",
                "aria-label*=\"email\"",
                "enter email",
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
        target_seed = raw_target_selector

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
        trace_group = self._record_step_trace_group(
            {
                "kind": "drag_fallback",
                "raw_source_selector": raw_source_selector,
                "raw_target_selector": raw_target_selector,
                "run_domain": run_domain,
                "source_candidates": list(source_candidates),
                "target_candidates": list(target_candidates),
                "attempts": [],
            }
        )

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

        trace_group["selector_pairs"] = [{"source": source, "target": target} for source, target in pairs]

        recovery_attempts = max(1, min(self._selector_recovery_attempts(), 2))
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
                        attempt_started = perf_counter()
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
                        trace_group["resolved_source_selector"] = source_selector
                        trace_group["resolved_target_selector"] = target_selector
                        trace_group["result"] = result
                        self._record_group_attempt(
                            trace_group,
                            {
                                "cycle": cycle,
                                "source_selector": source_selector,
                                "target_selector": target_selector,
                                "status": "success",
                                "elapsed_ms": round((perf_counter() - attempt_started) * 1000, 2),
                                "result": result,
                            },
                        )
                        return result
                    except Exception as exc:
                        last_error = exc
                        compact_error = self._compact_error(exc).lower()
                        timeout_like = isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or (
                            "timeout" in compact_error
                        )
                        self._record_group_attempt(
                            trace_group,
                            {
                                "cycle": cycle,
                                "source_selector": source_selector,
                                "target_selector": target_selector,
                                "status": "failed",
                                "elapsed_ms": round((perf_counter() - attempt_started) * 1000, 2),
                                "error": self._compact_error(exc),
                            },
                        )
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
            trace_group["final_error"] = self._compact_error(last_error)
            attempted = "; ".join(attempts[:8])
            suffix = " ..." if len(attempts) > 8 else ""
            raise ValueError(f"All drag selector pairs failed: {attempted}{suffix}") from last_error
        raise ValueError("Drag step failed with no selector attempts")

    def _selector_recovery_attempts(self) -> int:
        if not bool(getattr(self._settings, "selector_recovery_enabled", True)):
            return 1
        configured = int(getattr(self._settings, "selector_recovery_attempts", 2))
        return max(configured, 1)

    def _effective_selector_recovery_attempts(self, step_type: str, candidate_count: int) -> int:
        configured = self._selector_recovery_attempts()
        if candidate_count <= 1:
            return configured
        if step_type in {"click", "type", "select", "wait"} and candidate_count <= 8:
            return 1
        return configured

    async def _selector_recovery_pause(self) -> None:
        delay_ms = int(getattr(self._settings, "selector_recovery_delay_ms", 350))
        if delay_ms <= 0:
            return
        await asyncio.sleep(delay_ms / 1000)

    async def _smart_stabilization_wait(self, requested_ms: int) -> str:
        """
        Replace a fixed-ms timeout wait with DOM-readiness checks.

        Strategy:
        1. For short waits (≤ 3000ms): try wait_for_load_state('domcontentloaded').
           - Returns immediately if the page is already loaded (SPA case, very common).
           - Waits until DOMContentLoaded fires if a navigation just triggered.
        2. After DOM ready: sleep a small settle buffer (100–200ms) so SPA
           UI updates have time to apply before the next step runs.
        3. Fall back to the original fixed sleep if load_state check fails or
           the budget is large (likely an intentional long wait).
        """
        budget_ms = max(requested_ms, 100)
        if budget_ms > 3000:
            # Large waits are intentional pauses — don't second-guess them.
            await self._browser.wait_for(until="timeout", ms=budget_ms)
            return f"Waited {budget_ms}ms (intentional long wait)"

        started = perf_counter()
        dom_ready = False
        try:
            await asyncio.wait_for(
                self._browser.wait_for(
                    until="load_state",
                    load_state="domcontentloaded",
                    ms=budget_ms,
                ),
                timeout=max(budget_ms / 1000 + 0.5, 1.5),
            )
            dom_ready = True
        except Exception:
            pass

        if dom_ready:
            elapsed_ms = int((perf_counter() - started) * 1000)
            # Settle buffer: give SPA time to apply UI updates after DOM ready.
            # Capped at 200ms; never more than the original requested budget.
            settle_ms = max(100, min(budget_ms - elapsed_ms, 200))
            if settle_ms > 0:
                await asyncio.sleep(settle_ms / 1000)
            return f"DOM ready + {settle_ms}ms settle (smart wait, budget={budget_ms}ms)"

        # Fall back to original fixed sleep.
        await self._browser.wait_for(until="timeout", ms=budget_ms)
        return f"Waited {budget_ms}ms (timeout fallback)"

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
            "element is detached",
            "element is not visible",
            "element is outside of the viewport",
            "element is obscured",
            "intercept",
            "stale",
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
        if step_type == "click":
            text_button_match = re.fullmatch(r"button:has-text\((['\"])(.*?)\1\)", selector, re.IGNORECASE)
            if text_button_match:
                text_value = text_button_match.group(2).strip()
                escaped = self._escape_playwright_text(text_value)
                variants.extend(
                    [
                        f'a:has-text("{escaped}")',
                        f'[role="button"]:has-text("{escaped}")',
                        f':text-is("{escaped}")',
                        f"text={text_value}",
                    ]
                )
            text_link_match = re.fullmatch(r"a:has-text\((['\"])(.*?)\1\)", selector, re.IGNORECASE)
            if text_link_match:
                text_value = text_link_match.group(2).strip()
                escaped = self._escape_playwright_text(text_value)
                variants.extend(
                    [
                        f'button:has-text("{escaped}")',
                        f'[role="button"]:has-text("{escaped}")',
                        f':text-is("{escaped}")',
                        f"text={text_value}",
                    ]
                )
            aria_button_match = re.fullmatch(
                r"button\[aria-label=(['\"])(.*?)\1\]",
                selector,
                re.IGNORECASE,
            )
            if aria_button_match:
                aria_value = aria_button_match.group(2).strip()
                escaped = self._escape_playwright_text(aria_value)
                variants.extend(
                    [
                        f'[aria-label*="{escaped}"]',
                        f'[role="button"][aria-label*="{escaped}"]',
                        f'button[title*="{escaped}"]',
                    ]
                )
                if any(token in aria_value.lower() for token in ("language", "locale", "lang")):
                    variants.extend(self._merge_profile_candidates("language_switcher", {}, run_domain=None))
            amazon_container_like = (
                selector_lower.startswith("div")
                and any(
                    token in selector_lower
                    for token in ("s-widget-container", "celwidget", "desktop-grid-content-view", "search-results_")
                )
            )
            if amazon_container_like:
                variants.extend(
                    [
                        f"{selector} h2 a",
                        f"{selector} [data-cy='title-recipe-title'] a",
                        f"{selector} a[href*='/dp/']",
                        f"{selector} a.a-link-normal",
                    ]
                )

        if "s-main-slot" in selector_lower or "s-search-result" in selector_lower:
            amazon_position = self._amazon_result_position(selector_lower)
            if amazon_position is not None and amazon_position > 1:
                variants.extend(self._amazon_result_candidates(amazon_position))
            else:
                variants.extend(
                    [
                        "div[data-component-type='s-search-result'] h2 a",
                        "h2 a.a-link-normal",
                        "h2 a",
                    ]
                )
        elif step_type == "click":
            amazon_position = self._amazon_result_position(selector_lower)
            if amazon_position is not None:
                variants.extend(self._amazon_result_candidates(amazon_position))
        if "h2 a:visible" in selector_lower:
            variants.extend(
                [
                    "div[data-component-type='s-search-result'] h2 a",
                    "h2 a.a-link-normal",
                    "h2 a",
                ]
            )
        youtube_position = self._youtube_result_position(selector_lower)
        if youtube_position is not None:
            variants.extend(self._youtube_result_candidates(youtube_position))

        # ── ARIA-based expansion ──────────────────────────────────────────────
        # 1. text=XXX selectors: expand into role + text combinations so the
        #    executor tries semantic targets before falling back to recovery.
        text_sel_match = re.fullmatch(r"text=(.+)", selector, re.IGNORECASE)
        if text_sel_match:
            text_val = text_sel_match.group(1).strip().strip("\"'")
            esc = self._escape_playwright_text(text_val)
            if step_type == "click":
                variants.extend([
                    f"button:has-text('{esc}')",
                    f"[role='button']:has-text('{esc}')",
                    f"a:has-text('{esc}')",
                    f"[role='link']:has-text('{esc}')",
                    f"[role='menuitem']:has-text('{esc}')",
                    f"[role='tab']:has-text('{esc}')",
                    f"[aria-label*='{esc}']",
                    f"input[type='submit'][value*='{esc}']",
                ])
            elif step_type in {"type", "select"}:
                variants.extend([
                    f"input[aria-label='{esc}']",
                    f"input[aria-label*='{esc}']",
                    f"textarea[aria-label='{esc}']",
                    f"[role='textbox'][aria-label*='{esc}']",
                    f"input[placeholder='{esc}']",
                    f"input[placeholder*='{esc}']",
                    f"label:has-text('{esc}') + input",
                    f"label:has-text('{esc}') ~ input",
                ])

        # 2. label:has-text('X') input selectors: also try aria-label and
        #    placeholder attributes so any labelling pattern on the page works.
        label_input_match = re.match(
            r"label:has-text\((['\"])(.+?)\1\)\s*[~+]?\s*(?:input|textarea|select)",
            selector,
            re.IGNORECASE,
        )
        if label_input_match and step_type in {"type", "select"}:
            label_text = label_input_match.group(2).strip()
            esc = self._escape_playwright_text(label_text)
            variants.extend([
                f"input[aria-label='{esc}']",
                f"input[aria-label*='{esc}']",
                f"textarea[aria-label='{esc}']",
                f"[role='textbox'][aria-label*='{esc}']",
                f"input[placeholder='{esc}']",
                f"input[placeholder*='{esc}']",
            ])

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
    def _looks_like_transition_hint(value: str) -> bool:
        lowered = value.lower()
        return "transition" in lowered or "tranisition" in lowered

    @staticmethod
    def _transition_label_text_variants(value: str) -> list[str]:
        normalized = value.strip()
        if not normalized:
            return []
        variants = [normalized]
        corrected = re.sub(r"(?i)tranisition", "Transition", normalized)
        if corrected not in variants:
            variants.append(corrected)
        lower_corrected = re.sub(r"(?i)transition", "Tranisition", normalized)
        if lower_corrected not in variants:
            variants.append(lower_corrected)
        return variants

    def _transition_label_signal_selectors(self, text_hint: str, test_data: dict[str, Any]) -> list[str]:
        hinted_text = self._apply_template(text_hint, test_data).strip()
        candidates: list[str] = []
        seen: set[str] = set()
        for variant in self._transition_label_text_variants(hinted_text):
            for selector in (
                f"text={variant}",
                f"svg text:has-text(\"{self._escape_playwright_text(variant)}\")",
                f"[data-edge-label-renderer] :has-text(\"{self._escape_playwright_text(variant)}\")",
            ):
                if selector not in seen:
                    seen.add(selector)
                    candidates.append(selector)
        return candidates

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

    @classmethod
    def _merge_profile_candidates(
        cls,
        key: str,
        selector_profile: dict[str, list[str]],
        *,
        run_domain: str | None = None,
    ) -> list[str]:
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

    def _apply_template(self, text: str, test_data: dict[str, Any], *, run_id: str | None = None) -> str:
        if not text or "{{" not in text:
            return text

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            value = self._lookup_test_data_value(key, test_data)
            if value is None:
                if run_id:
                    run_context = self._run_context.get(run_id, {})
                    value = self._lookup_test_data_value(key, run_context)
            if value is None:
                builtin = self._resolve_builtin_template(key)
                if builtin is not None:
                    return builtin
                return match.group(0)
            return str(value)

        return TEMPLATE_PATTERN.sub(replace, text)

    def _run_context_selector_field_name(self, selector: str) -> str | None:
        alias_key = self._selector_alias_key(selector)
        if alias_key:
            return alias_key.replace(".", "_").replace("-", "_")

        selector_lower = selector.strip().lower()
        if not selector_lower:
            return None
        if "email" in selector_lower or "username" in selector_lower:
            return "email"
        if "confirm_password" in selector_lower or "confirm password" in selector_lower or "confirmpassword" in selector_lower:
            return "confirm_password"
        if "password" in selector_lower:
            return "password"
        if "workflow_name" in selector_lower or "workflow name" in selector_lower or "workflowname" in selector_lower:
            return "workflow_name"
        if "workflow_description" in selector_lower or "workflow description" in selector_lower:
            return "workflow_description"
        if "form_name" in selector_lower or "form name" in selector_lower or "formname" in selector_lower:
            return "form_name"
        if "label" in selector_lower:
            return "form_label"
        if "phone" in selector_lower or "tel" in selector_lower:
            return "phone"
        if "name" in selector_lower:
            return "name"
        return None

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

    @staticmethod
    def _should_request_selector_help(step: StepRuntimeState, exc: Exception) -> bool:
        if step.type not in {"click", "type", "select", "wait", "handle_popup", "verify_text"}:
            return False
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return step.type == "click"
        message = str(exc).lower()
        selector_failure_markers = (
            "all selector candidates failed",
            "no valid selector candidates",
            "no selector candidates available",
            "grounded selection failed",
            "grounded selection",
            "selector candidates",
        )
        actionable_markers = (
            "timeout",
            "waiting for",
            "locator.",
            "element",
            "not found",
            "not visible",
            "strict mode violation",
            "would receive the click",
            "unexpected token",
            "parsing css selector",
        )
        if step.type == "click":
            click_markers = selector_failure_markers + actionable_markers + (
                "click failed for",
                "locator.click",
                "not attached",
                "intercept",
                "another element would receive the click",
                "resolved to 0 elements",
                "blocked=",
                "in_iframe=",
            )
            return any(marker in message for marker in click_markers)
        if any(marker in message for marker in selector_failure_markers):
            return True
        return any(marker in message for marker in actionable_markers)

    @staticmethod
    def _requested_selector_target(step: StepRuntimeState) -> str | None:
        original = step.input.get("_selector_help_original")
        if isinstance(original, str) and original.strip():
            return original.strip()
        raw_selector = step.input.get("selector")
        if isinstance(raw_selector, str) and raw_selector.strip():
            return raw_selector.strip()
        return None

    def _build_selector_help_prompt(self, step: StepRuntimeState) -> str:
        requested = self._requested_selector_target(step) or "the missing element"
        action_label = step.type.replace("_", " ")
        last_attempt = ""
        if step.provided_selector:
            last_attempt = (
                f" The last selector you provided also did not work: {step.provided_selector}. "
                "Please try a different Playwright selector."
            )
        return (
            f"The agent could not find a working selector for the {action_label} step. "
            f"Please provide a Playwright selector for {requested} so the run can continue."
            f"{last_attempt}"
        )

    def _memory_candidates(self, run_domain: str | None, step_type: str, key: str) -> list[str]:
        store = self._selector_memory
        if not store or not run_domain:
            return []
        lookup_keys = self._selector_memory_lookup_keys(key)
        lookup_keys.extend(self._semantic_selector_memory_keys(step_type, key))
        lookup_keys = self._dedupe(lookup_keys)
        if not lookup_keys:
            return []
        max_items = max(int(getattr(self._settings, "selector_memory_max_candidates", 5)), 1)
        candidates: list[str] = []
        for key_token in lookup_keys:
            candidates.extend(store.get_candidates(run_domain, step_type, key_token, limit=max_items))
        filtered = [
            candidate
            for candidate in candidates
            if not self._is_unsafe_memory_selector(candidate)
        ]
        filtered = self._filter_memory_candidates(step_type, key, filtered)
        deduped = self._dedupe(filtered)
        return sorted(deduped, key=lambda candidate: self._memory_selector_priority(step_type, key, candidate))

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
        if step_type == "type":
            return
        if self._is_unsafe_memory_selector(resolved_selector):
            return

        keys = self._selector_memory_lookup_keys(raw_selector)
        alias = self._selector_alias_key(raw_selector)
        if alias:
            keys.extend(self._selector_memory_lookup_keys(alias))
        keys.extend(self._semantic_selector_memory_keys(step_type, raw_selector))

        selector_lower = raw_selector.lower()
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

    def _remember_selector_signature(
        self,
        *,
        run_domain: str | None,
        step_type: str,
        raw_selector: str,
        text_hint: str | None,
        signature: dict | None = None,
    ) -> None:
        """
        Persist the element signature alongside the same memory keys used by
        remember_success.  This allows signature-based re-identification when
        the raw selector stops working.

        *signature* may be supplied directly (e.g. extracted from a DOM snapshot
        after a successful execution when perception did not run).  If omitted,
        the method falls back to reading _grounded_signature from the active
        step's input (set by the perception layer).
        """
        store = self._selector_memory
        if not store or not run_domain:
            return

        if signature is None:
            active_step = self._active_step_state()
            if active_step is None:
                return
            signature = active_step.input.get("_grounded_signature")

        if not signature or not isinstance(signature, dict):
            return

        keys = self._selector_memory_lookup_keys(raw_selector)
        if text_hint:
            keys.extend(self._selector_memory_lookup_keys(text_hint))

        for key in self._dedupe(keys):
            store.remember_signature(run_domain, step_type, key, signature)

        LOGGER.debug(
            "Selector memory: stored element signature domain=%r step_type=%r raw_selector=%r",
            run_domain, step_type, raw_selector,
        )

    @staticmethod
    def _extract_signature_from_snapshot(
        selector: str,
        snapshot: dict[str, Any],
    ) -> dict | None:
        """
        Scan the element index built from *snapshot* and return the semantic
        signature (.signature()) of the element whose derived selectors include
        *selector*.

        Used to capture a signature after a successful execution even when the
        perception layer did not run (or returned an ambiguous match), so that
        future runs can use signature-based recovery without relying on the
        original selector string.

        Returns None when no element in the snapshot matches the selector.
        """
        if not selector or not snapshot:
            return None
        element_index = build_element_index(snapshot)
        selector_stripped = selector.strip()
        for el in element_index.elements:
            if selector_stripped in el.selectors:
                return el.signature()
        # Fallback: check if selector is a prefix-match of any element selector
        # (handles minor quoting differences such as ' vs ")
        sel_lower = selector_stripped.lower()
        for el in element_index.elements:
            for s in el.selectors:
                if s.lower() == sel_lower:
                    return el.signature()
        return None

    @staticmethod
    def _selector_memory_lookup_keys(key: str) -> list[str]:
        raw = key.strip()
        if not raw:
            return []

        compact = " ".join(raw.split())
        single_quoted = compact.replace('"', "'")
        variants = [raw, compact, single_quoted]
        lowered_variants = [item.lower() for item in variants]
        return AgentExecutor._dedupe(variants + lowered_variants)

    def _semantic_selector_memory_keys(self, step_type: str, selector: str) -> list[str]:
        if step_type not in {"click", "verify_text"}:
            return []

        text_value = self._extract_selector_text(selector)
        if not text_value:
            return []

        normalized = " ".join(text_value.strip().lower().split())
        if not normalized:
            return []

        return [
            f"text::{normalized}",
            f"{step_type}::text::{normalized}",
        ]

    @staticmethod
    def _extract_selector_text(selector: str) -> str | None:
        text = selector.strip()
        patterns = (
            r":has-text\((['\"])(.*?)\1\)",
            r":text\((['\"])(.*?)\1\)",
            r":text-is\((['\"])(.*?)\1\)",
            r"^text=(.+)$",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                value = match.group(2) if match.lastindex and match.lastindex >= 2 else match.group(1)
                normalized = value.strip().strip("'\"")
                if normalized:
                    return normalized
        return None

    @staticmethod
    def _is_unsafe_memory_selector(selector: str) -> bool:
        token = selector.strip().lower()
        return token in {
            "html",
            "body",
            "xpath=//html",
            "xpath=/html",
            "xpath=//body",
            "xpath=/body",
        } or token.startswith("html.") or token.startswith("body.")

    @staticmethod
    def _filter_memory_candidates(step_type: str, key: str, candidates: list[str]) -> list[str]:
        if step_type != "click":
            return candidates

        key_lower = key.strip().lower()
        field_source_aliases = {
            "short_answer_source",
            "{{selector.short_answer_source}}",
            "email_field_source",
            "{{selector.email_field_source}}",
            "dropdown_field_source",
            "{{selector.dropdown_field_source}}",
        }
        if key_lower in field_source_aliases:
            filtered = []
            for candidate in candidates:
                lowered = candidate.strip().lower()
                if any(token in lowered for token in ("placeholder='search'", 'placeholder="search"', "input[placeholder")):
                    continue
                if lowered.startswith(("input", "textarea", "select")):
                    continue
                if not any(
                    token in lowered
                    for token in (
                        "draggable='true'",
                        "data-rbd-draggable-id",
                        "data-testid='field-",
                        "data-testid*='field-",
                        "role='listitem'",
                        "has-text('short answer')",
                        'has-text("short answer")',
                        "has-text('email')",
                        'has-text("email")',
                        "has-text('dropdown')",
                        'has-text("dropdown")',
                        "text=short answer",
                        "text=email",
                        "text=dropdown",
                        "field-short-answer",
                        "field-email",
                        "field-dropdown",
                    )
                ):
                    continue
                filtered.append(candidate)
            return filtered or candidates

        expects_button_like_target = any(
            token in key_lower
            for token in (
                "button",
                ":has-text(",
                ":text(",
                "text=",
                "login",
                "sign up",
                "sign in",
                "workflows",
                "english",
                "de",
                "continue",
                "next",
                "submit",
                "save",
            )
        )
        if not expects_button_like_target:
            return candidates

        blocked_prefixes = ("input", "textarea", "select")
        blocked_fragments = (
            "@placeholder=",
            "[@placeholder=",
            "placeholder=",
            "enter email",
            "enter password",
        )
        filtered = []
        for candidate in candidates:
            lowered = candidate.strip().lower()
            if lowered.startswith(blocked_prefixes):
                continue
            if any(fragment in lowered for fragment in blocked_fragments):
                continue
            filtered.append(candidate)
        return filtered or candidates

    @staticmethod
    def _memory_selector_priority(step_type: str, key: str, selector: str) -> tuple[int, int, int, str]:
        lowered = selector.strip().lower()
        stability = 80

        if any(token in lowered for token in ("data-testid", "data-test", "data-qa")):
            stability = 0
        elif lowered.startswith("#") or "[id=" in lowered or "#".join(lowered.split()[:1]).startswith("#"):
            stability = 5
        elif any(token in lowered for token in ("[name=", "name='", 'name="', "aria-label", "role=")):
            stability = 10
        elif any(token in lowered for token in ("has-text(", ":text(", "text=", ":text-is(")):
            stability = 15
        elif lowered.startswith("xpath="):
            stability = 60

        if "\\." in lowered or lowered.count(".") >= 4:
            stability += 18
        if ":visible" in lowered:
            stability += 6
        if len(lowered) > 140:
            stability += 12

        key_lower = key.strip().lower()
        semantic_penalty = 0
        if step_type == "click":
            if any(token in key_lower for token in ("button", ":has-text(", ":text(", "text=", "login", "save", "submit")):
                if lowered.startswith(("input", "textarea", "select")) or "placeholder=" in lowered:
                    semantic_penalty = 40

        return (semantic_penalty, stability, len(lowered), lowered)

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
    def _duration_seconds(started_at: datetime | None, ended_at: datetime | None) -> float | None:
        if started_at is None or ended_at is None:
            return None
        return max((ended_at - started_at).total_seconds(), 0.0)

    @staticmethod
    def _format_seconds(value: float | None) -> str:
        if value is None:
            return "N/A"
        return f"{value:.2f}"

    @staticmethod
    def _run_status_meta(status: RunStatus) -> tuple[str, str]:
        if status == RunStatus.completed:
            return "Passed", "run-passed"
        if status == RunStatus.failed:
            return "Failed", "run-failed"
        if status == RunStatus.waiting_for_input:
            return "Needs Input", "run-skipped"
        if status == RunStatus.running:
            return "Running", "run-skipped"
        if status == RunStatus.cancelled:
            return "Cancelled", "run-skipped"
        return "Skipped", "run-skipped"

    @staticmethod
    def _step_status_meta(status: StepStatus) -> tuple[str, str]:
        if status == StepStatus.completed:
            return "Passed", "step-passed"
        if status == StepStatus.failed:
            return "Failed", "step-failed"
        if status == StepStatus.waiting_for_input:
            return "Needs Input", "step-skipped"
        if status == StepStatus.running:
            return "Running", "step-skipped"
        if status == StepStatus.cancelled:
            return "Cancelled", "step-skipped"
        if status == StepStatus.pending:
            return "Pending", "step-skipped"
        return "Skipped", "step-skipped"

    @staticmethod
    def _step_display_name(step: StepRuntimeState) -> str:
        payload = step.input or {}
        step_type = str(step.type).lower()

        if step_type == "navigate":
            return f"Navigate to {payload.get('url', '')}".strip()
        if step_type == "click":
            return f"Click {payload.get('selector', '')}".strip()
        if step_type == "type":
            return f"Type into {payload.get('selector', '')}".strip()
        if step_type == "select":
            selector = payload.get("selector", "")
            value = payload.get("value", "")
            return f"Select {value} in {selector}".strip()
        if step_type == "drag":
            source = payload.get("source_selector", "")
            target = payload.get("target_selector", "")
            return f"Drag {source} to {target}".strip()
        if step_type == "scroll":
            target = payload.get("target", "page")
            direction = payload.get("direction", "down")
            amount = payload.get("amount", 600)
            return f"Scroll {target} {direction} by {amount}px".strip()
        if step_type == "wait":
            return f"Wait ({payload.get('until', 'timeout')})".strip()
        if step_type == "handle_popup":
            return f"Handle popup ({payload.get('policy', 'dismiss')})".strip()
        if step_type == "verify_text":
            selector = payload.get("selector", "")
            value = payload.get("value", "")
            return f"Verify text '{value}' on {selector}".strip()
        if step_type == "verify_image":
            selector = payload.get("selector") or "page"
            return f"Verify image on {selector}".strip()

        return str(step.type)

    def _build_html_report(self, run: RunState) -> str:
        run_status_label, run_status_class = self._run_status_meta(run.status)
        run_duration = self._format_seconds(self._duration_seconds(run.started_at, run.finished_at))
        total_tests = len(run.steps)
        passed_tests = sum(1 for step in run.steps if step.status == StepStatus.completed)
        failed_tests = sum(1 for step in run.steps if step.status == StepStatus.failed)
        skipped_tests = total_tests - passed_tests - failed_tests

        step_items: list[str] = []
        for step in run.steps:
            step_status_label, step_status_class = self._step_status_meta(step.status)
            step_duration = self._format_seconds(self._duration_seconds(step.started_at, step.ended_at))
            step_name = escape(self._step_display_name(step))

            detail_parts: list[str] = []
            if step.message:
                detail_parts.append(f"Message: {step.message}")
            if step.error:
                detail_parts.append(f"Error: {step.error}")
            detail_text = escape(" | ".join(detail_parts))
            details_html = f'<div class="step-detail">{detail_text}</div>' if detail_text else ""
            screenshot_html = ""
            if step.status == StepStatus.failed and step.failure_screenshot:
                href = escape(step.failure_screenshot)
                screenshot_html = (
                    '<div class="step-detail">'
                    f'<a href="{href}" target="_blank" rel="noopener">View Screenshot</a>'
                    "</div>"
                )

            step_items.append(
                (
                    '<li class="step-item">'
                    f'<span class="tick {step_status_class}" aria-label="{escape(step_status_label)}">&#10003;</span>'
                    f'<span class="step-name">{step_name}</span>'
                    f'<span class="step-status">{escape(step_status_label)}</span>'
                    f'<span class="step-time">{escape(step_duration)}s</span>'
                    f"{details_html}"
                    f"{screenshot_html}"
                    "</li>"
                )
            )

        steps_html = "\n".join(step_items) if step_items else '<li class="step-item">No steps executed.</li>'

        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Test Run Report - {escape(run.run_name)}</title>
  <style>
    :root {{
      --bg: #000000;
      --card: #111111;
      --border: rgba(255, 255, 255, 0.1);
      --border-strong: rgba(255, 255, 255, 0.16);
      --text: #ffffff;
      --muted: #999999;
      --accent: #FFB300;
      --pass: #22c55e;
      --fail: #ef4444;
      --skip: #FFB300;
    }}
    body {{
      margin: 0;
      padding: 24px;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
    }}
    .report {{
      max-width: 980px;
      margin: 0 auto;
      background: var(--card);
      border: 1px solid var(--border-strong);
      border-radius: 12px;
      overflow: hidden;
      box-shadow: 0 6px 18px rgba(0, 0, 0, 0.5);
    }}
    .header {{
      padding: 16px 20px 10px;
      border-bottom: 1px solid var(--border);
      background: radial-gradient(circle at 10% -20%, rgba(255, 179, 0, 0.06), transparent 50%), var(--card);
    }}
    h1 {{
      margin: 0;
      font-size: 20px;
      color: #ffffff;
    }}
    h1 span {{
      color: var(--accent);
    }}
    .meta {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 13px;
    }}
    .overall {{
      display: grid;
      grid-template-columns: repeat(4, minmax(120px, 1fr));
      gap: 8px;
      padding: 12px 20px;
      border-bottom: 1px solid var(--border);
      background: #0d0d0d;
    }}
    .metric {{
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 8px 10px;
      background: #1a1a1a;
    }}
    .metric-label {{
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.03em;
    }}
    .metric-value {{
      margin-top: 4px;
      font-size: 18px;
      font-weight: 700;
      color: #ffffff;
    }}
    .metric-pass .metric-value {{
      color: #22c55e;
    }}
    .metric-fail .metric-value {{
      color: #ef4444;
    }}
    .metric-skip .metric-value {{
      color: #FFB300;
    }}
    details {{
      border-top: 1px solid var(--border);
    }}
    details:first-of-type {{
      border-top: none;
    }}
    summary {{
      list-style: none;
      cursor: pointer;
      display: grid;
      gap: 12px;
      grid-template-columns: minmax(220px, 1.6fr) minmax(160px, 1fr) minmax(130px, 0.8fr);
      align-items: center;
      padding: 14px 20px;
      user-select: none;
      transition: background 0.15s ease;
    }}
    summary:hover {{
      background: rgba(255, 179, 0, 0.04);
    }}
    summary::-webkit-details-marker {{
      display: none;
    }}
    .summary-title {{
      font-weight: 600;
      color: #ffffff;
    }}
    .status-cell {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }}
    .status-pill {{
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.02em;
    }}
    .run-passed {{
      background: rgba(34, 197, 94, 0.15);
      color: #22c55e;
    }}
    .run-failed {{
      background: rgba(239, 68, 68, 0.15);
      color: #ef4444;
    }}
    .run-skipped {{
      background: rgba(255, 179, 0, 0.15);
      color: #FFB300;
    }}
    .arrow {{
      color: var(--muted);
      transition: transform 0.15s ease;
      display: inline-block;
    }}
    details[open] .arrow {{
      transform: rotate(90deg);
    }}
    .seconds {{
      font-weight: 600;
      color: var(--accent);
    }}
    .steps {{
      margin: 0;
      padding: 6px 20px 18px 20px;
      list-style: none;
      display: grid;
      gap: 8px;
    }}
    .step-item {{
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 10px;
      padding: 10px 12px;
      display: grid;
      gap: 8px;
      grid-template-columns: 18px minmax(180px, 1fr) minmax(80px, auto) minmax(80px, auto);
      align-items: center;
      background: #151515;
      transition: border-color 0.15s ease;
    }}
    .step-item:hover {{
      border-color: rgba(255, 179, 0, 0.2);
    }}
    .tick {{
      font-size: 15px;
      font-weight: 800;
      line-height: 1;
    }}
    .step-passed {{
      color: #22c55e;
    }}
    .step-failed {{
      color: #ef4444;
    }}
    .step-skipped {{
      color: #FFB300;
    }}
    .step-name {{
      font-size: 14px;
      color: #e0e0e0;
    }}
    .step-status,
    .step-time {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.02em;
    }}
    .step-detail {{
      grid-column: 2 / -1;
      color: var(--muted);
      font-size: 12px;
    }}
    .step-detail a {{
      color: var(--accent);
      text-decoration: underline;
    }}
    @media (max-width: 820px) {{
      .overall {{
        grid-template-columns: repeat(2, minmax(120px, 1fr));
      }}
    }}
  </style>
</head>
<body>
  <main class="report">
    <section class="header">
      <h1>Test <span>Execution</span> Report</h1>
      <div class="meta">Run ID: {escape(run.run_id)}</div>
    </section>
    <section class="overall">
      <div class="metric">
        <div class="metric-label">Total Tests</div>
        <div class="metric-value">{total_tests}</div>
      </div>
      <div class="metric metric-pass">
        <div class="metric-label">Test Passed</div>
        <div class="metric-value">{passed_tests}</div>
      </div>
      <div class="metric metric-fail">
        <div class="metric-label">Test Failed</div>
        <div class="metric-value">{failed_tests}</div>
      </div>
      <div class="metric metric-skip">
        <div class="metric-label">Test Skipped</div>
        <div class="metric-value">{skipped_tests}</div>
      </div>
    </section>
    <details>
      <summary>
        <span class="summary-title">Test Case Name: {escape(run.run_name)}</span>
        <span class="status-cell">
          <span class="status-pill {run_status_class}">Status: {escape(run_status_label)}</span>
          <span class="arrow" aria-hidden="true">&#9656;</span>
        </span>
        <span class="seconds">Execution Time (seconds): {escape(run_duration)}</span>
      </summary>
      <ul class="steps">
        {steps_html}
      </ul>
    </details>
  </main>
</body>
</html>
"""

    @staticmethod
    def _build_summary(run) -> str:
        completed = sum(1 for step in run.steps if step.status == StepStatus.completed)
        failed = sum(1 for step in run.steps if step.status == StepStatus.failed)
        cancelled = sum(1 for step in run.steps if step.status == StepStatus.cancelled)
        return (
            f"Run '{run.run_name}' ended with status {run.status}. "
            f"Completed={completed}, Failed={failed}, Cancelled={cancelled}."
        )
