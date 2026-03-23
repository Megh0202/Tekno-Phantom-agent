from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest

from app.runtime.executor import AgentExecutor
from app.runtime.selector_memory import InMemorySelectorMemoryStore


def _executor(step_timeout_seconds: int = 15) -> AgentExecutor:
    executor = AgentExecutor.__new__(AgentExecutor)
    executor._settings = SimpleNamespace(
        step_timeout_seconds=step_timeout_seconds,
        selector_recovery_enabled=True,
        selector_recovery_attempts=2,
        selector_recovery_delay_ms=0,
    )
    executor._selector_memory = None
    return executor


def test_selector_candidates_use_default_email_profile() -> None:
    executor = _executor()
    candidates = executor._selector_candidates(
        raw_selector="input[type='email']",
        step_type="type",
        selector_profile={},
        test_data={},
        run_domain=None,
        text_hint="qa@example.com",
    )

    assert candidates[0] == "#username"
    assert "input[name='username']" in candidates
    assert "input[type='email']" in candidates


def test_selector_fallback_tries_multiple_candidates() -> None:
    executor = _executor(step_timeout_seconds=6)
    attempted: list[str] = []

    async def operation(selector: str) -> str:
        attempted.append(selector)
        if selector == "input[name='username']":
            return f"Typed into {selector} (after clear)"
        raise ValueError(f"Missing {selector}")

    result = asyncio.run(
        executor._run_with_selector_fallback(
            raw_selector="input[type='email']",
            step_type="type",
            selector_profile={},
            test_data={},
            run_domain=None,
            operation=operation,
            text_hint="qa@example.com",
        )
    )

    assert result == "Typed into input[name='username'] (after clear)"
    assert attempted[0] == "#username"
    assert attempted[1] == "input[name='username']"


def test_selector_fallback_error_lists_attempts() -> None:
    executor = _executor(step_timeout_seconds=4)

    async def operation(selector: str) -> str:
        raise ValueError(f"Missing {selector}")

    with pytest.raises(ValueError) as exc_info:
        asyncio.run(
            executor._run_with_selector_fallback(
                raw_selector="input[type='email']",
                step_type="type",
                selector_profile={},
                test_data={},
                run_domain=None,
                operation=operation,
                text_hint="qa@example.com",
            )
        )

    message = str(exc_info.value)
    assert "All selector candidates failed" in message
    assert "#username" in message
    assert "input[type='email']" in message


def test_selector_variants_include_id_case_and_contains_conversions() -> None:
    executor = _executor()
    candidates = executor._selector_candidates(
        raw_selector='button#create_form:contains("Create Form")',
        step_type="click",
        selector_profile={},
        test_data={},
        run_domain=None,
    )

    assert 'button#create_form:contains("Create Form")' in candidates
    assert 'button#createForm:contains("Create Form")' in candidates
    assert 'button#create_form:has-text("Create Form")' in candidates
    assert "text=Create Form" in candidates


def test_selector_variants_include_amazon_result_fallbacks() -> None:
    executor = _executor()
    candidates = executor._selector_candidates(
        raw_selector="div.s-main-slot div[data-index='0'] h2 a",
        step_type="click",
        selector_profile={},
        test_data={},
        run_domain=None,
    )

    assert "div[data-component-type='s-search-result'] h2 a" in candidates
    assert "h2 a.a-link-normal" in candidates


def test_selector_candidates_include_amazon_result_defaults_for_h2_visible() -> None:
    executor = _executor()
    candidates = executor._selector_candidates(
        raw_selector="h2 a:visible",
        step_type="click",
        selector_profile={},
        test_data={},
        run_domain=None,
    )

    assert "div[data-component-type='s-search-result'] h2 a" in candidates
    assert "h2 a.a-link-normal" in candidates
    assert "h2 a" in candidates


def test_selector_candidates_include_amazon_add_to_cart_defaults() -> None:
    executor = _executor()
    candidates = executor._selector_candidates(
        raw_selector="button:has-text('Add to Cart')",
        step_type="click",
        selector_profile={},
        test_data={},
        run_domain=None,
    )

    assert "#add-to-cart-button" in candidates
    assert "input[name='submit.add-to-cart']" in candidates


def test_selector_candidates_include_form_name_defaults() -> None:
    executor = _executor()
    candidates = executor._selector_candidates(
        raw_selector="input[name='formName']",
        step_type="type",
        selector_profile={},
        test_data={},
        run_domain=None,
        text_hint="QA_Form_20260223_154500",
    )

    assert "input[name='formName']" in candidates
    assert "input[name='name']" in candidates
    assert "input#formName" in candidates


def test_selector_candidates_include_create_form_defaults() -> None:
    executor = _executor()
    candidates = executor._selector_candidates(
        raw_selector="button#create_form",
        step_type="verify_text",
        selector_profile={},
        test_data={},
        run_domain=None,
    )

    assert "button#createForm" in candidates
    assert "button:has-text('Create Form')" in candidates


def test_selector_candidates_include_module_switch_defaults() -> None:
    executor = _executor()
    launcher_candidates = executor._selector_candidates(
        raw_selector="{{selector.module_launcher}}",
        step_type="click",
        selector_profile={},
        test_data={},
        run_domain=None,
    )
    workflow_candidates = executor._selector_candidates(
        raw_selector="{{selector.module_workflows}}",
        step_type="click",
        selector_profile={},
        test_data={},
        run_domain=None,
    )

    assert "button[aria-label*='module']" in launcher_candidates
    assert "a[href*='/workflows/definitions']" in workflow_candidates
    assert "text=Workflows" in workflow_candidates


def test_verify_text_hint_promotes_create_form_candidates() -> None:
    executor = _executor()
    candidates = executor._selector_candidates(
        raw_selector="h1",
        step_type="verify_text",
        selector_profile={},
        test_data={},
        run_domain=None,
        text_hint="Create Form",
    )

    assert candidates[0] == "button#createForm"
    assert "button:has-text('Create Form')" in candidates
    assert "h1" in candidates


def test_verify_text_hint_promotes_login_candidates() -> None:
    executor = _executor()
    candidates = executor._selector_candidates(
        raw_selector="body",
        step_type="verify_text",
        selector_profile={},
        test_data={},
        run_domain=None,
        text_hint="Login successful",
    )

    assert "button[name='login']" in candidates
    assert "button[type='submit']" in candidates
    assert "body" in candidates


def test_selector_candidates_include_drag_defaults() -> None:
    executor = _executor()
    source_candidates = executor._selector_candidates(
        raw_selector="short answer",
        step_type="drag",
        selector_profile={},
        test_data={},
        run_domain=None,
    )
    target_candidates = executor._selector_candidates(
        raw_selector="form canvas",
        step_type="drag",
        selector_profile={},
        test_data={},
        run_domain=None,
    )

    assert "[draggable='true']:has-text('Short answer')" in source_candidates
    assert ".form-canvas" in target_candidates
    assert "[data-testid='form-builder-canvas']" in target_candidates


def test_selector_candidates_include_form_label_defaults() -> None:
    executor = _executor()
    candidates = executor._selector_candidates(
        raw_selector="[data-testid='form-builder-canvas'] input[name='label']",
        step_type="type",
        selector_profile={},
        test_data={},
        run_domain=None,
        text_hint="First Name",
    )

    assert "[data-testid='form-builder-canvas'] input[placeholder='Label']" in candidates
    assert "textarea[placeholder='Label']" in candidates


def test_apply_template_expands_now_macro() -> None:
    executor = _executor()
    output = executor._apply_template("QA_Form_{{NOW_YYYYMMDD_HHMMSS}}", {})
    assert re.match(r"^QA_Form_\d{8}_\d{6}$", output)


def test_selector_memory_prioritizes_previous_successes() -> None:
    executor = _executor()
    memory = InMemorySelectorMemoryStore()
    memory.remember_success("test.vitaone.io", "click", "create_form", "button#createForm")
    memory.remember_success("test.vitaone.io", "click", "create_form", "button#createForm")
    memory.remember_success("test.vitaone.io", "click", "create_form", "button#create_form")
    executor._selector_memory = memory

    candidates = executor._selector_candidates(
        raw_selector="button#create_form",
        step_type="click",
        selector_profile={},
        test_data={},
        run_domain="test.vitaone.io",
    )

    assert candidates[0] == "button#createForm"


def test_selector_fallback_retries_transient_timeout_and_recovers() -> None:
    executor = _executor(step_timeout_seconds=4)
    call_count = 0

    async def operation(selector: str) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise TimeoutError("Timeout 15000ms exceeded")
        return f"Clicked {selector}"

    result = asyncio.run(
        executor._run_with_selector_fallback(
            raw_selector="#onlytarget",
            step_type="click",
            selector_profile={},
            test_data={},
            run_domain=None,
            operation=operation,
        )
    )

    assert result == "Clicked #onlytarget"
    assert call_count == 2


def test_email_candidates_exclude_password_selectors_from_memory() -> None:
    executor = _executor()
    memory = InMemorySelectorMemoryStore()
    memory.remember_success("test.vitaone.io", "type", "email", "#password")
    memory.remember_success("test.vitaone.io", "type", "email", "input[name='username']")
    executor._selector_memory = memory

    candidates = executor._selector_candidates(
        raw_selector="email field",
        step_type="type",
        selector_profile={},
        test_data={},
        run_domain="test.vitaone.io",
        text_hint="qa@example.com",
    )

    assert "#password" not in candidates
    assert "input[name='username']" in candidates


def test_password_value_with_at_symbol_does_not_trigger_email_candidates() -> None:
    executor = _executor()
    candidates = executor._selector_candidates(
        raw_selector="password field",
        step_type="type",
        selector_profile={},
        test_data={},
        run_domain=None,
        text_hint="PasswordVitaone1@",
    )

    assert "#password" in candidates
    assert "#username" not in candidates


def test_selector_fallback_does_not_retry_non_transient_error() -> None:
    executor = _executor(step_timeout_seconds=4)
    call_count = 0

    async def operation(selector: str) -> str:
        nonlocal call_count
        call_count += 1
        raise ValueError("No option with value '2' found")

    with pytest.raises(ValueError) as exc_info:
        asyncio.run(
            executor._run_with_selector_fallback(
                raw_selector="#onlytarget",
                step_type="select",
                selector_profile={},
                test_data={},
                run_domain=None,
                operation=operation,
            )
        )

    assert "All selector candidates failed" in str(exc_info.value)
    assert call_count == 1


def test_drag_fallback_retries_transient_timeout_and_recovers() -> None:
    executor = _executor(step_timeout_seconds=4)

    class _Browser:
        def __init__(self) -> None:
            self.calls = 0

        async def drag_and_drop(self, source_selector: str, target_selector: str) -> str:
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("Timeout 15000ms exceeded")
            return f"Dragged {source_selector} to {target_selector}"

    browser = _Browser()
    executor._browser = browser

    result = asyncio.run(
        executor._run_with_drag_fallback(
            raw_source_selector="#source",
            raw_target_selector="#target",
            selector_profile={},
            test_data={},
            run_domain=None,
        )
    )

    assert result == "Dragged #source to #target"
    assert browser.calls == 2
