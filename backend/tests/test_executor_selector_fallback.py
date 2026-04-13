from __future__ import annotations

import asyncio
import re
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

from app.runtime.executor import AgentExecutor, CandidateConfidence
from app.runtime.perception import build_element_index, find_best_match
from app.runtime.selector_memory import InMemorySelectorMemoryStore
from app.schemas import RunState, RunStatus, StepRuntimeState, StepStatus


def _executor(step_timeout_seconds: int = 15) -> AgentExecutor:
    executor = AgentExecutor.__new__(AgentExecutor)
    executor._settings = SimpleNamespace(
        step_timeout_seconds=step_timeout_seconds,
        selector_recovery_enabled=True,
        selector_recovery_attempts=2,
        selector_recovery_delay_ms=0,
        execution_fast_path_enabled=True,
        execution_fast_path_action_timeout_seconds=4,
        execution_fast_path_selector_timeout_ms=2000,
    )
    executor._selector_memory = None
    executor._step_trace_context = ContextVar("executor_step_trace_context_test", default=None)
    executor._step_state_context = ContextVar("executor_step_state_context_test", default=None)
    return executor


class _BrowserStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.values: dict[str, str] = {}

    async def navigate(self, url: str) -> str:
        self.calls.append(("navigate", url))
        return f"Navigated to {url}"

    async def click(self, selector: str) -> str:
        self.calls.append(("click", selector))
        return f"Clicked {selector}"

    async def type_text(self, selector: str, text: str, clear_first: bool = True) -> str:
        self.calls.append(("type_text", (selector, text, clear_first)))
        self.values[selector] = text
        return f"Typed into {selector} (after clear)"

    async def select(self, selector: str, value: str) -> str:
        self.calls.append(("select", (selector, value)))
        self.values[selector] = value
        return f"Selected {value} in {selector}"

    async def wait_for(
        self,
        until: str,
        ms: int | None = None,
        selector: str | None = None,
        load_state: str | None = None,
    ) -> str:
        self.calls.append(("wait_for", (until, ms, selector, load_state)))
        if until == "timeout":
            return f"Waited {ms}ms"
        return f"Waited for {until}"

    async def inspect_page(self, include_screenshot: bool = True) -> dict[str, object]:
        self.calls.append(("inspect_page", include_screenshot))
        return {
            "url": "https://example.com",
            "title": "Example",
            "text_excerpt": "Example page",
            "interactive_elements": [],
            "page_count": 1,
        }

    async def assess_click_effect(
        self,
        selector: str,
        before_snapshot: dict[str, object] | None = None,
        raw_selector: str | None = None,
        text_hint: str | None = None,
        target_context: dict[str, object] | None = None,
    ) -> dict[str, str]:
        self.calls.append(("assess_click_effect", selector))
        return {
            "status": "passed",
            "detail": f"Mock click accepted for {selector}",
        }

    async def get_element_context(self, selector: str) -> dict[str, object] | None:
        self.calls.append(("get_element_context", selector))
        return {"selector": selector, "text": selector, "title": "", "aria": "", "href": ""}

    async def get_element_value(self, selector: str) -> str | None:
        self.calls.append(("get_element_value", selector))
        return self.values.get(selector)

    async def get_select_value(self, selector: str) -> str | None:
        self.calls.append(("get_select_value", selector))
        return self.values.get(selector)


class _RunStore:
    def __init__(self, run: RunState | None = None) -> None:
        self._run = run

    def get(self, run_id: str) -> RunState | None:
        if self._run and self._run.run_id == run_id:
            return self._run
        return None

    def persist(self, run: RunState) -> None:
        self._run = run


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

    assert candidates[0] == "input[type='email']"
    assert "input[name='username']" in candidates
    assert "input[type='email']" in candidates


def test_selector_candidates_include_signup_alias_defaults() -> None:
    executor = _executor()

    assert "input[name='name']" in executor._selector_candidates(
        raw_selector="{{selector.first_name}}",
        step_type="type",
        selector_profile={},
        test_data={},
        run_domain=None,
        text_hint="Test01",
    )
    assert "input[name='surname']" in executor._selector_candidates(
        raw_selector="{{selector.surname}}",
        step_type="type",
        selector_profile={},
        test_data={},
        run_domain=None,
        text_hint="Last01",
    )
    assert "input[type='tel']" in executor._selector_candidates(
        raw_selector="{{selector.phone}}",
        step_type="type",
        selector_profile={},
        test_data={},
        run_domain=None,
        text_hint="91991919919",
    )
    assert "input[name='confirm_password']" in executor._selector_candidates(
        raw_selector="{{selector.confirm_password}}",
        step_type="type",
        selector_profile={},
        test_data={},
        run_domain=None,
        text_hint="Abcd@1234",
    )


def test_duplicate_ids_do_not_become_grounded_or_snapshot_primary_selectors() -> None:
    executor = _executor()
    snapshot = {
        "url": "https://atozbay-demo.aercjbp.com/signup",
        "interactive_elements": [
            {"tag": "input", "id": "floatingInput", "name": "name", "selectors": ["#floatingInput"], "visible": True, "enabled": True},
            {"tag": "input", "id": "floatingInput", "name": "surname", "selectors": ["#floatingInput"], "visible": True, "enabled": True},
            {"tag": "input", "id": "floatingInput", "name": "email", "selectors": ["#floatingInput"], "visible": True, "enabled": True},
        ],
    }

    candidates = executor._page_snapshot_selector_candidates(
        snapshot,
        "{{selector.surname}}",
        "type",
        "Last01",
    )
    assert candidates
    assert candidates[0] == 'input[name="surname"]'
    assert "#floatingInput" not in candidates[:3]

    perception = find_best_match("surname", "type", build_element_index(snapshot))
    assert perception is not None
    assert perception.selector == "input[name='surname']"


def test_type_selector_candidates_prioritize_explicit_selector_before_email_aliases() -> None:
    executor = _executor()
    candidates = executor._selector_candidates(
        raw_selector="input[name='email']",
        step_type="type",
        selector_profile={},
        test_data={},
        run_domain=None,
        text_hint="qa@example.com",
    )

    assert candidates[0] == "input[name='email']"
    assert "#username" in candidates


def test_password_candidates_do_not_infer_email_from_password_value() -> None:
    executor = _executor()
    candidates = executor._selector_candidates(
        raw_selector="input[name='password']",
        step_type="type",
        selector_profile={},
        test_data={},
        run_domain=None,
        text_hint="Madhu@123",
    )

    assert candidates[0] == "input[name='password']"
    assert "input[placeholder*='Email']" not in candidates


def test_type_candidates_do_not_infer_email_profile_from_email_like_value() -> None:
    executor = _executor()
    candidates = executor._selector_candidates(
        raw_selector="input[placeholder='First Name']",
        step_type="type",
        selector_profile={},
        test_data={},
        run_domain="example.com",
        text_hint="test@example.com",
    )

    assert "input[name='email']" not in candidates
    assert "input[placeholder*='Email']" not in candidates


def test_memory_candidates_match_selector_case_and_quote_variants() -> None:
    executor = _executor()
    memory = InMemorySelectorMemoryStore()
    memory.remember_success(
        "app.stag.dr-adem.com",
        "click",
        "button:has-text('english')",
        "button:has-text('English')",
    )
    executor._selector_memory = memory

    candidates = executor._memory_candidates(
        "app.stag.dr-adem.com",
        "click",
        'button:has-text("English")',
    )

    assert "button:has-text('English')" in candidates


def test_click_memory_candidates_match_across_text_selector_forms() -> None:
    executor = _executor()
    memory = InMemorySelectorMemoryStore()
    memory.remember_success(
        "app.stag.dr-adem.com",
        "click",
        "text::sign up",
        "button.text-\\[12px\\].font-ibm-plex.font-medium.text-black.underline.ml-1.hover\\:opacity-80:visible",
    )
    executor._selector_memory = memory

    candidates = executor._memory_candidates(
        "app.stag.dr-adem.com",
        "click",
        'button:has-text("Sign Up")',
    )

    assert "button.text-\\[12px\\].font-ibm-plex.font-medium.text-black.underline.ml-1.hover\\:opacity-80:visible" in candidates


def test_click_alias_candidates_prefer_remembered_selector_before_profile_defaults() -> None:
    executor = _executor()
    memory = InMemorySelectorMemoryStore()
    memory.remember_success(
        "app.stag.dr-adem.com",
        "click",
        "login_button",
        "[data-testid='login-button']",
    )
    executor._selector_memory = memory

    candidates = executor._selector_candidates(
        raw_selector="{{selector.login_button}}",
        step_type="click",
        selector_profile={},
        test_data={},
        run_domain="app.stag.dr-adem.com",
    )

    assert candidates[0] == "[data-testid='login-button']"
    assert "button:has-text('Login')" in candidates


def test_memory_candidates_skip_unsafe_root_level_selectors() -> None:
    executor = _executor()
    memory = InMemorySelectorMemoryStore()
    memory.remember_success("app.stag.dr-adem.com", "click", "cta", "xpath=//body")
    memory.remember_success("app.stag.dr-adem.com", "click", "cta", "button:has-text('Continue')")
    executor._selector_memory = memory

    candidates = executor._memory_candidates("app.stag.dr-adem.com", "click", "cta")

    assert "xpath=//body" not in candidates
    assert "button:has-text('Continue')" in candidates


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
    assert attempted[0] == "input[type='email']"
    assert attempted[1] == "input[name='email']"
    assert "input[name='username']" in attempted


def test_selector_fallback_prefers_profile_candidates_before_live_dom_rerank_without_grounding() -> None:
    executor = _executor(step_timeout_seconds=6)

    class _Browser:
        async def inspect_page(self, include_screenshot: bool = True) -> dict[str, object]:
            return {
                "url": "https://example.com/login",
                "title": "Login",
                "text_excerpt": "Email",
                "interactive_elements": [
                    {
                        "tag": "input",
                        "role": "textbox",
                        "text": "",
                        "name": "emailLive",
                        "id": "live-email",
                        "visible": True,
                        "enabled": True,
                        "scope": "form",
                    }
                ],
                "page_count": 1,
            }

        async def wait_for(
            self,
            until: str,
            ms: int | None = None,
            selector: str | None = None,
            load_state: str | None = None,
        ) -> str:
            return f"Waited for {selector}"

    executor._browser = _Browser()
    attempted: list[str] = []

    async def operation(selector: str) -> str:
        attempted.append(selector)
        if selector == "input[type='email']":
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

    assert result == "Typed into input[type='email'] (after clear)"
    assert attempted[0] == "input[type='email']"
    assert "#live-email" not in attempted


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


def test_selector_fallback_retries_when_post_validation_fails_and_records_trace() -> None:
    executor = _executor(step_timeout_seconds=6)
    step = StepRuntimeState(index=0, type="type", input={"type": "type", "selector": "input[type='email']"})
    trace: dict[str, object] = {"attempt_groups": []}
    trace_token = executor._step_trace_context.set(trace)
    step_token = executor._step_state_context.set(step)
    attempted: list[str] = []

    async def operation(selector: str) -> str:
        attempted.append(selector)
        return f"Typed into {selector} (after clear)"

    async def post_validate(selector: str, result: str, pre: object) -> str | None:
        if selector == "input[type='email']":
            raise ValueError("Type validation failed for input[type='email']: expected value 'qa@example.com' but found ''.")
        return "post_validation=passed (value='qa@example.com')"

    try:
        result = asyncio.run(
            executor._run_with_selector_fallback(
                raw_selector="input[type='email']",
                step_type="type",
                selector_profile={},
                test_data={},
                run_domain=None,
                operation=operation,
                text_hint="qa@example.com",
                post_validate=post_validate,
            )
        )
    finally:
        executor._step_trace_context.reset(trace_token)
        executor._step_state_context.reset(step_token)

    assert result.endswith("post_validation=passed (value='qa@example.com')")
    assert attempted[:2] == ["input[type='email']", "input[name='email']"]
    group = trace["attempt_groups"][0]
    assert group["final_selected_selector"] == "input[name='email']"
    assert group["resolved_selector"] == "input[name='email']"
    assert "selector_generation_ms" in group
    assert "live_candidate_lookup_ms" in group
    assert group["attempts"][0]["status"] == "failed"
    assert "validation failed" in group["attempts"][0]["error"].lower()
    assert "elapsed_ms" in group["attempts"][0]
    assert "browser_action_ms" in group["attempts"][0]


def test_classify_execution_path_prefers_fast_path_for_simple_type_selector() -> None:
    executor = _executor()
    run = RunState(
        run_id="run-fast-type",
        run_name="Fast Type",
        status=RunStatus.pending,
        steps=[],
        selector_profile={},
        test_data={},
    )
    step = StepRuntimeState(
        index=0,
        type="type",
        input={"type": "type", "selector": "#searchInput", "text": "Artificial intelligence"},
    )

    classification = executor._classify_execution_path(run, step)

    assert classification["path"] == "fast"
    assert classification["reason"] == "simple_selector"
    assert classification["primary_selector"] == "#searchInput"


def test_classify_execution_path_prefers_grounded_selector_over_alias_slow_path() -> None:
    executor = _executor()
    run = RunState(
        run_id="run-grounded-fast",
        run_name="Grounded Fast",
        status=RunStatus.pending,
        steps=[],
        selector_profile={},
        test_data={},
    )
    step = StepRuntimeState(
        index=0,
        type="click",
        input={
            "type": "click",
            "selector": "{{selector.create_form}}",
            "_grounded_selector": "#createForm",
        },
    )

    classification = executor._classify_execution_path(run, step)

    assert classification["path"] == "fast"
    assert classification["reason"] == "grounded_selector"
    assert classification["primary_selector"] == "#createForm"


def test_build_step_intent_captures_element_type_target_and_ordinal() -> None:
    executor = _executor()

    intent = executor._build_step_intent(
        "click",
        "[data-component-type='s-search-result']:not([data-sponsored='true']):nth-of-type(2) h2 a",
        None,
    )

    assert intent.action == "click"
    assert intent.element_type == "listitem"
    assert intent.ordinal == 2
    assert intent.raw_selector == "[data-component-type='s-search-result']:not([data-sponsored='true']):nth-of-type(2) h2 a"


def test_rank_selectors_by_intent_prefers_button_candidates_for_button_intent() -> None:
    executor = _executor()
    intent = executor._build_step_intent("click", "Search button", None)

    ranked = executor._rank_selectors_by_intent(
        [
            "input[type='search']",
            "a:has-text('Search')",
            "button:has-text('Search')",
        ],
        intent,
    )

    assert ranked[0] == "button:has-text('Search')"


def test_rank_selectors_by_intent_penalizes_clear_search_query_for_search_button() -> None:
    executor = _executor()
    intent = executor._build_step_intent("click", "Search button", None)

    ranked = executor._rank_selectors_by_intent(
        [
            'button[aria-label*="Clear search query"]',
            '#search-icon-legacy',
            'button[aria-label*="Search"]',
        ],
        intent,
    )

    assert ranked[0] != 'button[aria-label*="Clear search query"]'


def test_selector_candidates_drop_clear_search_query_for_search_button_intent() -> None:
    executor = _executor()

    candidates = executor._selector_candidates(
        raw_selector="button[id='search-icon-legacy']",
        step_type="click",
        selector_profile={},
        test_data={},
        run_domain="www.youtube.com",
    )

    assert 'button[aria-label*="Clear search query"]' not in candidates


def test_rank_selectors_by_intent_prefers_indexed_result_candidate() -> None:
    executor = _executor()
    intent = executor._build_step_intent(
        "click",
        "[data-component-type='s-search-result']:not([data-sponsored='true']):nth-of-type(2) h2 a",
        None,
    )

    ranked = executor._rank_selectors_by_intent(
        [
            "[data-component-type='s-search-result']:not([data-sponsored='true']) h2 a",
            "[data-component-type='s-search-result']:nth-of-type(2) .a-link-normal",
            "[data-component-type='s-search-result']:nth-of-type(2) h2 a",
        ],
        intent,
    )

    assert ranked[0] == "[data-component-type='s-search-result']:nth-of-type(2) h2 a"


def test_selector_candidates_include_youtube_result_variants() -> None:
    executor = _executor()
    candidates = executor._selector_candidates(
        raw_selector="ytd-video-renderer:first-of-type a[id='video-title']",
        step_type="click",
        selector_profile={},
        test_data={},
        run_domain="www.youtube.com",
    )

    assert "a#video-title >> nth=0" in candidates
    assert "ytd-video-renderer a#video-title >> nth=0" in candidates


def test_confidence_gate_marks_precise_button_candidates_high_confidence() -> None:
    executor = _executor()
    intent = executor._build_step_intent("click", "Search button", None)

    narrowed, confidence = executor._confidence_gate_candidates(
        [
            "button:has-text('Search')",
            "a:has-text('Search')",
            "input[type='search']",
        ],
        intent,
        step_type="click",
        source="initial",
    )

    assert confidence.level == "high"
    assert narrowed[0] == "button:has-text('Search')"
    assert len(narrowed) <= 3


def test_confidence_gate_keeps_low_confidence_amazon_results_narrow() -> None:
    executor = _executor()
    intent = executor._build_step_intent(
        "click",
        "[data-component-type='s-search-result']:not([data-sponsored='true']):nth-of-type(2) h2 a",
        None,
    )

    narrowed, confidence = executor._confidence_gate_candidates(
        [
            "[data-component-type='s-search-result']:not([data-sponsored='true']) h2 a",
            "[data-component-type='s-search-result']:nth-of-type(2) .a-link-normal",
            "div[data-component-type='s-search-result']:nth-of-type(2) [data-cy='title-recipe-title'] a",
            "div[data-component-type='s-search-result'] h2 a >> nth=1",
        ],
        intent,
        step_type="click",
        source="live",
    )

    assert confidence.level in {"low", "medium", "high"}
    assert "[data-component-type='s-search-result']:nth-of-type(2) .a-link-normal" not in narrowed
    assert all("nth-of-type(2)" in selector or ">> nth=1" in selector for selector in narrowed)


def test_candidate_execution_policy_disables_broad_recovery_for_high_confidence() -> None:
    executor = _executor()
    policy = executor._candidate_execution_policy(
        CandidateConfidence(level="high", top_score=50, second_score=34, score_gap=16, retained_count=2),
        step_type="click",
    )

    assert policy["mode"] == "direct"
    assert policy["recovery_attempts"] == 1
    assert policy["allow_live_candidates"] is False
    assert policy["allow_llm_recovery"] is False


def test_candidate_execution_policy_keeps_low_confidence_recovery_enabled() -> None:
    executor = _executor()
    policy = executor._candidate_execution_policy(
        CandidateConfidence(level="low", top_score=12, second_score=10, score_gap=2, retained_count=3),
        step_type="click",
    )

    assert policy["mode"] == "recovering"
    assert policy["allow_live_candidates"] is True
    assert policy["allow_llm_recovery"] is True


def test_type_alias_candidates_do_not_include_unresolved_template_literal() -> None:
    executor = _executor()

    candidates = executor._selector_candidates(
        "{{selector.password}}",
        "type",
        {},
        {},
        "test.vitaone.io",
        text_hint="PasswordVitaone1@",
    )

    assert "{{selector.password}}" not in candidates
    assert "input[name='password']" in candidates


def test_form_name_alias_filters_out_generic_name_candidates() -> None:
    executor = _executor()

    candidates = executor._selector_candidates(
        "{{selector.form_name}}",
        "type",
        {},
        {},
        "test.vitaone.io",
        text_hint="QA_Form_20260401_114001",
    )

    assert "input#name" in candidates
    assert "input[name='name']" in candidates
    assert "input[placeholder='Enter a name']" in candidates


def test_form_name_alias_filters_out_search_and_login_candidates() -> None:
    executor = _executor()

    filtered = executor._filter_alias_candidates(
        "form_name",
        [
            "div[role='dialog'] input[placeholder='Enter a name']",
            "input[placeholder*='Search']",
            "input[name='username']",
            "input[type='password']",
        ],
    )

    assert filtered == ["div[role='dialog'] input[placeholder='Enter a name']"]


@pytest.mark.asyncio
async def test_find_element_from_intent_requires_keyword_match_for_form_name() -> None:
    executor = _executor()
    executor._browser = _BrowserStub()

    async def inspect_page(include_screenshot: bool = True) -> dict[str, object]:
        return {
            "url": "https://test.vitaone.io/forms",
            "title": "Forms",
            "text_excerpt": "Create form",
            "interactive_elements": [
                {
                    "tag": "input",
                    "placeholder": "Search",
                    "name": "search",
                    "visible": True,
                    "enabled": True,
                },
                {
                    "tag": "input",
                    "placeholder": "Enter a name",
                    "name": "name",
                    "visible": True,
                    "enabled": True,
                },
            ],
            "page_count": 1,
        }

    executor._browser.inspect_page = inspect_page  # type: ignore[method-assign]

    candidates = await executor._find_element_from_intent(
        step_type="type",
        raw_selector="{{selector.form_name}}",
        text_hint="QA_Form_20260401_154143",
        selector_profile={},
        test_data={},
        run_domain="test.vitaone.io",
    )

    assert any("name" in candidate.lower() for candidate in candidates)
    assert not any("search" in candidate.lower() for candidate in candidates)


def test_scope_score_prefers_main_for_list_items() -> None:
    executor = _executor()
    intent = executor._build_step_intent("click", "second product result", None)

    main_score = executor._scope_score("main", intent)
    nav_score = executor._scope_score("nav", intent)

    assert main_score > nav_score


def test_page_snapshot_selector_candidates_prefer_main_scoped_button() -> None:
    executor = _executor()
    snapshot = {
        "interactive_elements": [
            {
                "tag": "button",
                "text": "Play",
                "aria": "",
                "name": "",
                "id": "nav-play",
                "testid": "",
                "role": "button",
                "placeholder": "",
                "href": "",
                "title": "",
                "scope": "nav",
            },
            {
                "tag": "button",
                "text": "Play",
                "aria": "",
                "name": "",
                "id": "main-play",
                "testid": "",
                "role": "button",
                "placeholder": "",
                "href": "",
                "title": "",
                "scope": "main",
            },
        ]
    }

    candidates = executor._page_snapshot_selector_candidates(snapshot, "Play button", "click", None)

    assert "#main-play" in candidates
    assert "#nav-play" not in candidates


def test_page_snapshot_selector_candidates_prefer_unique_stable_button_over_duplicate_labels() -> None:
    executor = _executor()
    snapshot = {
        "interactive_elements": [
                {
                    "tag": "button",
                    "text": "Play",
                    "aria": "",
                    "name": "",
                    "id": "",
                "testid": "",
                "role": "button",
                "placeholder": "",
                "href": "",
                "title": "",
                "scope": "header",
            },
                {
                    "tag": "button",
                    "text": "Play",
                    "aria": "",
                    "name": "",
                    "id": "main-play",
                    "testid": "main-play-button",
                    "role": "button",
                "placeholder": "",
                "href": "",
                "title": "",
                "scope": "main",
            },
        ]
    }

    candidates = executor._page_snapshot_selector_candidates(snapshot, "Play button", "click", None)

    assert "#main-play" in candidates
    assert candidates[0] in {'#main-play', '[data-testid="main-play-button"]'}


def test_page_snapshot_selector_candidates_skip_hidden_button() -> None:
    executor = _executor()
    snapshot = {
        "interactive_elements": [
            {
                "tag": "button",
                "text": "Play",
                "aria": "",
                "name": "",
                "id": "hidden-play",
                "testid": "",
                "role": "button",
                "placeholder": "",
                "href": "",
                "title": "",
                "scope": "main",
                "visible": False,
                "enabled": True,
            },
            {
                "tag": "button",
                "text": "Play",
                "aria": "",
                "name": "",
                "id": "visible-play",
                "testid": "",
                "role": "button",
                "placeholder": "",
                "href": "",
                "title": "",
                "scope": "main",
                "visible": True,
                "enabled": True,
            },
        ]
    }

    candidates = executor._page_snapshot_selector_candidates(snapshot, "Play button", "click", None)

    assert "#visible-play" in candidates
    assert "#hidden-play" not in candidates


def test_snapshot_match_score_penalizes_disabled_button() -> None:
    executor = _executor()
    intent = executor._build_step_intent("click", "Play button", None)
    enabled_score = executor._snapshot_match_score(
        {
            "tag": "button",
            "text": "Play",
            "role": "button",
            "scope": "main",
            "visible": True,
            "enabled": True,
        },
        ["play"],
        "click",
        intent,
        {"play": 1},
    )
    disabled_score = executor._snapshot_match_score(
        {
            "tag": "button",
            "text": "Play",
            "role": "button",
            "scope": "main",
            "visible": True,
            "enabled": False,
        },
        ["play"],
        "click",
        intent,
        {"play": 1},
    )

    assert enabled_score > disabled_score


def test_page_snapshot_selector_candidates_choose_second_ranked_list_item_by_dom_order() -> None:
    executor = _executor()
    snapshot = {
        "interactive_elements": [
            {
                "tag": "a",
                "text": "Kurti Product One",
                "aria": "",
                "name": "",
                "id": "product-one",
                "testid": "",
                "role": "link",
                "placeholder": "",
                "href": "/p1",
                "title": "Kurti Product One",
                "scope": "main",
            },
            {
                "tag": "a",
                "text": "Kurti Product Two",
                "aria": "",
                "name": "",
                "id": "product-two",
                "testid": "",
                "role": "link",
                "placeholder": "",
                "href": "/p2",
                "title": "Kurti Product Two",
                "scope": "main",
            },
        ]
    }

    candidates = executor._page_snapshot_selector_candidates(snapshot, "click the second product result", "click", None)

    assert "#product-two" in candidates
    assert "#product-one" not in candidates


def test_page_snapshot_selector_candidates_use_precomputed_title_link_selectors() -> None:
    executor = _executor()
    snapshot = {
        "interactive_elements": [
            {
                "tag": "a",
                "text": "Product One",
                "aria": "",
                "name": "",
                "id": "",
                "testid": "",
                "role": "link",
                "placeholder": "",
                "href": "/p1",
                "title": "Product One",
                "scope": "main",
                "selectors": ["div[data-component-type='s-search-result'] h2 a >> nth=0"],
            },
            {
                "tag": "a",
                "text": "Product Two",
                "aria": "",
                "name": "",
                "id": "",
                "testid": "",
                "role": "link",
                "placeholder": "",
                "href": "/p2",
                "title": "Product Two",
                "scope": "main",
                "selectors": ["div[data-component-type='s-search-result'] h2 a >> nth=1"],
            },
        ]
    }

    candidates = executor._page_snapshot_selector_candidates(snapshot, "click the second product result", "click", None)

    assert "div[data-component-type='s-search-result'] h2 a >> nth=1" in candidates
    assert "div[data-component-type='s-search-result'] h2 a >> nth=0" not in candidates


def test_dispatch_fast_step_types_and_validates_without_fallback() -> None:
    executor = _executor()
    executor._browser = _BrowserStub()
    executor._selector_memory = None
    trace: dict[str, object] = {"attempt_groups": []}
    step = StepRuntimeState(
        index=0,
        type="type",
        input={"type": "type", "selector": "#searchInput", "text": "Artificial intelligence"},
    )
    run = RunState(
        run_id="run-fast-dispatch",
        run_name="Fast Dispatch",
        status=RunStatus.pending,
        steps=[],
        selector_profile={},
        test_data={},
    )
    trace_token = executor._step_trace_context.set(trace)
    step_token = executor._step_state_context.set(step)
    try:
        result = asyncio.run(
            executor._dispatch_fast_step(
                run,
                step.input,
                {"path": "fast", "reason": "simple_selector", "primary_selector": "#searchInput", "candidate_count": 1},
            )
        )
    finally:
        executor._step_trace_context.reset(trace_token)
        executor._step_state_context.reset(step_token)

    assert "post_validation=passed" in result
    assert executor._browser.calls[0][0] == "type_text"
    assert executor._browser.calls[1][0] == "get_element_value"
    group = trace["attempt_groups"][0]
    assert group["kind"] == "fast_path"
    assert group["final_selected_selector"] == "#searchInput"
    assert group["attempts"][0]["status"] == "success"
    assert "elapsed_ms" in group["attempts"][0]
    assert "post_validate_ms" in group["attempts"][0]
    assert step.provided_selector == "#searchInput"


def test_execute_step_switches_to_waiting_for_input_on_selector_failure() -> None:
    executor = _executor(step_timeout_seconds=4)
    executor._settings.selector_help_mode = "pause"

    async def _raise_selector_error(run: RunState, raw_step: dict) -> str:
        raise ValueError("All selector candidates failed: pass 1: button:has-text('Workflows') -> timeout")

    async def _noop(*args, **kwargs):
        return None

    executor._dispatch_step = _raise_selector_error
    executor._files = SimpleNamespace(
        write_text_artifact=_noop,
        write_bytes_artifact=_noop,
    )
    executor._capture_failure_screenshot = lambda *args, **kwargs: asyncio.sleep(0)

    run = RunState(run_name="selector-help-run", steps=[])
    step = StepRuntimeState(
        index=0,
        type="click",
        input={"type": "click", "selector": "button:has-text('Workflows')"},
    )

    asyncio.run(executor._execute_step(run, step))

    assert step.status == StepStatus.waiting_for_input
    assert step.user_input_kind == "selector"
    assert "Please provide a Playwright selector" in (step.user_input_prompt or "")
    assert step.requested_selector_target == "button:has-text('Workflows')"


def test_execute_existing_steps_does_not_block_on_waiting_for_input_step() -> None:
    executor = _executor(step_timeout_seconds=4)
    run = RunState(
        run_name="selector-help-continue-run",
        steps=[
            StepRuntimeState(
                index=0,
                type="click",
                input={"type": "click", "selector": "button:has-text('Workflows')"},
                status=StepStatus.waiting_for_input,
                user_input_kind="selector",
                requested_selector_target="button:has-text('Workflows')",
            ),
            StepRuntimeState(
                index=1,
                type="wait",
                input={"type": "wait", "until": "timeout", "ms": 50},
                status=StepStatus.pending,
            ),
        ],
    )
    executor._run_store = _RunStore(run)

    async def _execute_step(current_run: RunState, step: StepRuntimeState) -> None:
        step.status = StepStatus.completed
        step.message = f"Executed step {step.index}"

    executor._execute_step = _execute_step  # type: ignore[method-assign]

    has_step_failure = asyncio.run(executor._execute_existing_steps(run))

    assert has_step_failure is True
    assert run.steps[0].status == StepStatus.waiting_for_input
    assert run.steps[1].status == StepStatus.completed
    assert run.steps[1].message == "Executed step 1"


def test_execute_step_continues_to_selector_pipeline_after_page_assertion_failure() -> None:
    executor = _executor(step_timeout_seconds=4)
    executor._settings.selector_help_mode = "pause"

    async def _raise_page_assertion(*args, **kwargs):
        raise ValueError("Page state assertion failed before type: no element matched intent 'phone' on the current page.")

    async def _dispatch_step(run: RunState, raw_step: dict) -> str:
        raise ValueError("All selector candidates failed: pass 1: input[type='tel'] -> timeout")

    async def _noop(*args, **kwargs):
        return None

    executor._assert_step_page_state = _raise_page_assertion
    executor._dispatch_step = _dispatch_step
    executor._safe_page_snapshot = _noop
    executor._check_page_health = lambda *args, **kwargs: {"status": "ok", "issues": []}
    executor._files = SimpleNamespace(
        write_text_artifact=_noop,
        write_bytes_artifact=_noop,
    )
    executor._capture_failure_screenshot = lambda *args, **kwargs: asyncio.sleep(0)

    run = RunState(run_name="selector-help-run", steps=[])
    step = StepRuntimeState(
        index=0,
        type="type",
        input={"type": "type", "selector": "{{selector.phone}}", "text": "91991919919"},
    )

    asyncio.run(executor._execute_step(run, step))

    assert step.status == StepStatus.waiting_for_input
    assert step.user_input_kind == "selector"
    assert step.requested_selector_target == "{{selector.phone}}"


def test_click_selector_parse_error_still_requests_selector_help() -> None:
    executor = _executor(step_timeout_seconds=4)
    executor._settings.selector_help_mode = "pause"

    async def _raise_selector_error(run: RunState, raw_step: dict) -> str:
        raise ValueError(
            'All selector candidates failed: pass 1: page.get_by_text("Workflows", exact=True) '
            '-> Unexpected token "get_by_text(" while parsing css selector'
        )

    async def _noop(*args, **kwargs):
        return None

    executor._dispatch_step = _raise_selector_error
    executor._files = SimpleNamespace(
        write_text_artifact=_noop,
        write_bytes_artifact=_noop,
    )
    executor._capture_failure_screenshot = lambda *args, **kwargs: asyncio.sleep(0)

    run = RunState(run_name="selector-help-run", steps=[])
    step = StepRuntimeState(
        index=0,
        type="click",
        input={"type": "click", "selector": 'page.get_by_text("Workflows", exact=True)'},
    )

    asyncio.run(executor._execute_step(run, step))

    assert step.status == StepStatus.waiting_for_input
    assert step.user_input_kind == "selector"


def test_execute_type_step_fails_on_plain_timeout_without_selector_resolution_error() -> None:
    executor = _executor(step_timeout_seconds=1)

    async def _hang(run: RunState, raw_step: dict) -> str:
        await asyncio.sleep(2)
        return "never"

    executor._dispatch_step = _hang
    executor._files = SimpleNamespace(
        write_text_artifact=lambda *args, **kwargs: None,
        write_bytes_artifact=lambda *args, **kwargs: None,
    )
    executor._capture_failure_screenshot = lambda *args, **kwargs: asyncio.sleep(0)

    run = RunState(run_name="selector-help-run", steps=[])
    step = StepRuntimeState(
        index=0,
        type="type",
        input={"type": "type", "selector": "input[name='email']", "text": "qa@example.com"},
    )

    asyncio.run(executor._execute_step(run, step))

    assert step.status == StepStatus.failed
    assert step.user_input_kind is None
    assert step.requested_selector_target is None


def test_apply_manual_selector_hint_updates_step_without_premature_selector_memory() -> None:
    executor = _executor()
    memory = InMemorySelectorMemoryStore()
    run = RunState(
        run_id="run-1",
        run_name="selector-help-run",
        start_url="https://test.vitaone.io/workflows",
        status=RunStatus.waiting_for_input,
        steps=[
            StepRuntimeState(
                step_id="step-1",
                index=0,
                type="click",
                input={"type": "click", "selector": "button:has-text('Workflows')"},
                status=StepStatus.waiting_for_input,
                user_input_kind="selector",
                requested_selector_target="button:has-text('Workflows')",
            )
        ],
    )
    executor._run_store = _RunStore(run)
    executor._selector_memory = memory

    updated = executor.apply_manual_selector_hint("run-1", "step-1", "a:has-text('Workflows')")

    assert updated is not None
    step = updated.steps[0]
    assert step.status == StepStatus.pending
    assert step.input["selector"] == "a:has-text('Workflows')"
    assert step.provided_selector == "a:has-text('Workflows')"
    remembered = memory.get_candidates("test.vitaone.io", "click", "button:has-text('Workflows')")
    assert remembered == []
    assert step.input["_selector_help_original"] == "button:has-text('Workflows')"


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


def test_click_text_selector_variants_include_link_and_text_fallbacks() -> None:
    executor = _executor()
    candidates = executor._selector_candidates(
        raw_selector="button:has-text('Sign Up')",
        step_type="click",
        selector_profile={},
        test_data={},
        run_domain=None,
    )

    assert "button:has-text('Sign Up')" in candidates
    assert 'a:has-text("Sign Up")' in candidates
    assert '[role="button"]:has-text("Sign Up")' in candidates
    assert ':text-is("Sign Up")' in candidates
    assert "text=Sign Up" in candidates


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


def test_selector_candidates_prioritize_amazon_second_result_targets() -> None:
    executor = _executor()
    candidates = executor._selector_candidates(
        raw_selector="[data-component-type='s-search-result']:nth-of-type(2) h2 a",
        step_type="click",
        selector_profile={},
        test_data={},
        run_domain="www.amazon.in",
    )

    assert candidates[0] == "div[data-component-type='s-search-result'] h2 a >> nth=1"
    assert "div[data-component-type='s-search-result']:nth-of-type(2) h2 a" in candidates
    assert "h2 a.a-link-normal" not in candidates
    assert "h2 a" not in candidates


@pytest.mark.asyncio
async def test_assert_step_page_state_fails_when_natural_language_target_is_not_on_current_page() -> None:
    executor = _executor()
    snapshot = {
        "url": "https://example.com/login",
        "title": "Login",
        "text_excerpt": "Sign in to continue",
        "interactive_elements": [
            {
                "tag": "input",
                "role": "textbox",
                "text": "",
                "name": "username",
                "id": "username",
                "visible": True,
                "enabled": True,
                "scope": "form",
            },
            {
                "tag": "button",
                "role": "button",
                "text": "Sign In",
                "name": "login",
                "id": "login",
                "visible": True,
                "enabled": True,
                "scope": "form",
            },
        ],
        "page_count": 1,
    }
    step = StepRuntimeState(
        index=0,
        type="click",
        input={"type": "click", "selector": "Create Form"},
    )
    intent = executor._build_step_intent("click", "Create Form")

    with pytest.raises(ValueError, match="Page state assertion failed before click"):
        await executor._assert_step_page_state(
            RunState(run_name="guard-test", steps=[]),
            step,
            snapshot,
            intent,
        )


@pytest.mark.asyncio
async def test_assert_step_page_state_skips_explicit_selector_without_live_match() -> None:
    executor = _executor()
    snapshot = {
        "url": "https://example.com/login",
        "title": "Login",
        "text_excerpt": "Sign in to continue",
        "interactive_elements": [],
        "page_count": 1,
    }
    step = StepRuntimeState(
        index=0,
        type="click",
        input={"type": "click", "selector": "#create-form"},
    )
    intent = executor._build_step_intent("click", "#create-form")

    result = await executor._assert_step_page_state(
        RunState(run_name="guard-test", steps=[]),
        step,
        snapshot,
        intent,
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "explicit_selector"


def test_step_intent_infers_amazon_result_ordinal_from_widget_selector() -> None:
    executor = _executor()

    intent = executor._build_step_intent(
        "click",
        "div[class='s-widget-container celwidget' widgetId='search-results_2'] div[class='desktop-grid-content-view']",
    )

    assert intent.element_type == "listitem"
    assert intent.ordinal == 2


def test_selector_candidates_prioritize_clickable_descendants_for_amazon_container_selector() -> None:
    executor = _executor()
    raw_selector = (
        "div[class='s-widget-container s-spacing-small s-widget-container-height-small celwidget "
        "slot=MAIN template=SEARCH_RESULTS widgetId=search-results_2'] "
        "div[class='a-section a-spacing-base desktop-grid-content-view']"
    )

    candidates = executor._selector_candidates(
        raw_selector=raw_selector,
        step_type="click",
        selector_profile={},
        test_data={},
        run_domain="www.amazon.in",
    )

    assert candidates[0] == "div[data-component-type='s-search-result'] h2 a >> nth=1"
    assert f"{raw_selector} h2 a" in candidates
    assert candidates.index(raw_selector) > candidates.index("div[data-component-type='s-search-result'] h2 a >> nth=1")


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


def test_click_post_validation_rejects_search_result_click_without_navigation() -> None:
    executor = _executor()
    captured: dict[str, object] = {}

    class _Browser:
        async def assess_click_effect(
            self,
            selector: str,
            before_snapshot: dict[str, object] | None = None,
            raw_selector: str | None = None,
            text_hint: str | None = None,
            target_context: dict[str, object] | None = None,
        ) -> dict[str, object]:
            captured["selector"] = selector
            captured["raw_selector"] = raw_selector
            captured["text_hint"] = text_hint
            captured["target_context"] = target_context
            return {
                "status": "failed",
                "detail": "Search-result click did not leave the results page or open a new product page/tab.",
            }

    executor._browser = _Browser()

    with pytest.raises(ValueError) as exc_info:
        asyncio.run(
            executor._validate_click_post_action(
                resolved_selector="div[data-component-type='s-search-result'] h2 a >> nth=1",
                before_snapshot={
                    "page_snapshot": {"url": "https://www.amazon.in/s?k=kurti", "page_count": 1},
                    "element_context": {"text": "SHANTI TERRA KART", "title": "SHANTI TERRA KART"},
                },
                raw_selector="[data-component-type='s-search-result']:nth-of-type(2) h2 a",
                text_hint="second non-sponsored product",
            )
        )

    assert "did not leave the results page" in str(exc_info.value)
    assert captured["raw_selector"] == "[data-component-type='s-search-result']:nth-of-type(2) h2 a"
    assert captured["text_hint"] == "second non-sponsored product"
    assert captured["target_context"] == {"text": "SHANTI TERRA KART", "title": "SHANTI TERRA KART"}


def test_capture_click_pre_state_collects_page_and_element_context() -> None:
    executor = _executor()

    class _Browser:
        async def inspect_page(self, include_screenshot: bool = True) -> dict[str, object]:
            return {"url": "https://example.com", "title": "Example"}

        async def get_element_context(self, selector: str) -> dict[str, object] | None:
            return {"selector": selector, "text": "Example card title"}

    executor._browser = _Browser()

    pre_state = asyncio.run(executor._capture_click_pre_state("a#video-title"))

    assert pre_state["page_snapshot"]["url"] == "https://example.com"
    assert pre_state["element_context"]["text"] == "Example card title"


def test_llm_selector_recovery_filters_broad_amazon_second_result_suggestions() -> None:
    executor = _executor()

    filtered = executor._filter_llm_selector_suggestions(
        step_type="click",
        failed_selector="[data-component-type='s-search-result']:not([data-sponsored='true']):nth-of-type(2) h2 a",
        suggestions=[
            "[data-component-type='s-search-result']:not([data-sponsored='true']) h2 a",
            "[data-component-type='s-search-result']:nth-of-type(2) .a-link-normal",
            "div[data-component-type='s-search-result'] h2 a >> nth=1",
            "div[data-component-type='s-search-result']:nth-of-type(2) [data-cy='title-recipe-title'] a",
        ],
    )

    assert "[data-component-type='s-search-result']:not([data-sponsored='true']) h2 a" not in filtered
    assert "[data-component-type='s-search-result']:nth-of-type(2) .a-link-normal" not in filtered
    assert "div[data-component-type='s-search-result'] h2 a >> nth=1" in filtered
    assert "div[data-component-type='s-search-result']:nth-of-type(2) [data-cy='title-recipe-title'] a" in filtered


def test_llm_recovery_is_blocked_when_active_policy_disallows_it() -> None:
    executor = _executor()
    executor._brain = SimpleNamespace(suggest_selectors=lambda **kwargs: ["button:has-text('Search')"])
    trace = {
        "attempt_groups": [
            {
                "kind": "selector_fallback",
                "execution_policy": {
                    "mode": "direct",
                    "allow_live_candidates": False,
                    "allow_llm_recovery": False,
                },
            }
        ]
    }
    run = RunState(run_id="run-1", run_name="Run 1", status=RunStatus.pending, steps=[], selector_profile={}, test_data={})
    step = StepRuntimeState(
        index=0,
        type="click",
        input={"type": "click", "selector": "Search button"},
    )
    token = executor._step_trace_context.set(trace)
    try:
        recovered = asyncio.run(executor._try_llm_selector_recovery(run, step, ValueError("All selector candidates failed")))
    finally:
        executor._step_trace_context.reset(token)

    assert recovered is None


def test_selector_candidates_include_form_name_defaults() -> None:
    executor = _executor()
    candidates = executor._selector_candidates(
        raw_selector="input[name='formName']",
        step_type="type",
        selector_profile={},
        test_data={},
        run_domain="test.vitaone.io",
        text_hint="QA_Form_20260223_154500",
    )

    assert "input[name='formName']" in candidates
    assert "input[name='name']" in candidates
    assert "input#name" in candidates


def test_selector_candidates_include_create_form_defaults() -> None:
    executor = _executor()
    candidates = executor._selector_candidates(
        raw_selector="button#create_form",
        step_type="verify_text",
        selector_profile={},
        test_data={},
        run_domain="test.vitaone.io",
    )

    assert "button#createForm" in candidates
    assert "button:has-text('Create Form')" in candidates


def test_verify_text_hint_promotes_create_form_candidates() -> None:
    executor = _executor()
    candidates = executor._selector_candidates(
        raw_selector="h1",
        step_type="verify_text",
        selector_profile={},
        test_data={},
        run_domain="test.vitaone.io",
        text_hint="Create Form",
    )

    assert candidates[0] in {"button#createForm", "button:has-text('Create Form')"}
    assert "button:has-text('Create Form')" in candidates
    assert "h1" in candidates


def test_generic_runs_do_not_load_vitaone_only_candidates() -> None:
    executor = _executor()
    candidates = executor._selector_candidates(
        raw_selector="button#create_form",
        step_type="verify_text",
        selector_profile={},
        test_data={},
        run_domain="www.wikipedia.org",
    )

    assert "button:has-text('Create Form')" not in candidates


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
        run_domain="test.vitaone.io",
    )
    target_candidates = executor._selector_candidates(
        raw_selector="form canvas",
        step_type="drag",
        selector_profile={},
        test_data={},
        run_domain="test.vitaone.io",
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
        run_domain="test.vitaone.io",
        text_hint="First Name",
    )

    assert "[data-testid='form-builder-canvas'] input[placeholder='Label']" in candidates
    assert "textarea[placeholder='Label']" in candidates


def test_apply_template_expands_now_macro() -> None:
    executor = _executor()
    output = executor._apply_template("QA_Form_{{NOW_YYYYMMDD_HHMMSS}}", {})
    assert re.match(r"^QA_Form_\d{8}_\d{6}$", output)


def test_initialize_runtime_test_data_stabilizes_now_templates() -> None:
    executor = _executor()
    data = executor._initialize_runtime_test_data({})

    first = executor._apply_template("InitialState_{{NOW_YYYYMMDD_HHMMSS}}", data)
    second = executor._apply_template("SubmittedState_{{NOW_YYYYMMDD_HHMMSS}}", data)

    assert re.match(r"^InitialState_\d{8}_\d{6}$", first)
    assert re.match(r"^SubmittedState_\d{8}_\d{6}$", second)
    assert first.split("_", 1)[1] == second.split("_", 1)[1]


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


def test_selector_memory_prefers_stable_click_selector_over_brittle_css() -> None:
    executor = _executor()
    memory = InMemorySelectorMemoryStore()
    memory.remember_success(
        "test.vitaone.io",
        "click",
        "button:has-text('Login')",
        "button.flex.items-center.gap-1\\.5.rounded-md.text-white:visible",
    )
    memory.remember_success(
        "test.vitaone.io",
        "click",
        "button:has-text('Login')",
        "button:has-text('Login')",
    )
    executor._selector_memory = memory

    candidates = executor._memory_candidates(
        "test.vitaone.io",
        "click",
        "button:has-text('Login')",
    )

    assert candidates[0] == "button:has-text('Login')"


def test_click_candidate_timeout_is_capped_for_single_candidate() -> None:
    executor = _executor(step_timeout_seconds=60)

    assert executor._candidate_timeout_seconds(1, step_type="click") == 3.0


def test_type_candidate_timeout_is_capped_for_single_candidate() -> None:
    executor = _executor(step_timeout_seconds=60)

    assert executor._candidate_timeout_seconds(1, step_type="type") == 2.5


def test_simple_actions_use_single_selector_cycle() -> None:
    executor = _executor()

    assert executor._effective_selector_recovery_attempts("click", 4) == 1
    assert executor._effective_selector_recovery_attempts("type", 4) == 1
    assert executor._effective_selector_recovery_attempts("select", 4) == 1
    assert executor._effective_selector_recovery_attempts("wait", 4) == 1
    assert executor._effective_selector_recovery_attempts("click", 12) == 2


def test_explicit_selector_detection_keeps_only_strong_stable_selectors() -> None:
    executor = _executor()

    assert executor._looks_like_explicit_selector("#username") is True
    assert executor._looks_like_explicit_selector("input[name='search_query']") is True
    assert executor._looks_like_explicit_selector("xpath=//button[@type='submit']") is True
    assert executor._looks_like_explicit_selector("ytd-video-renderer:first-of-type a[id='video-title']") is False
    assert executor._looks_like_explicit_selector("a:has-text('Logout')") is False
    assert executor._looks_like_explicit_selector("input[name='email'], input[type='email'], #email") is False


def test_simple_type_skips_live_candidates() -> None:
    executor = _executor()

    assert executor._should_skip_live_candidates("type", "input[name='search']", None, 4) is True
    assert executor._should_skip_live_candidates("click", "#nav-search-submit-button", None, 4) is True


def test_dynamic_dom_error_forces_live_candidate_rerank() -> None:
    executor = _executor()
    error = ValueError("Element is detached from DOM")

    assert executor._is_dynamic_dom_error(error) is True
    assert executor._should_skip_live_candidates("type", "input[name='search']", error, 4) is False


def test_should_retry_selector_error_treats_stale_detached_as_retryable() -> None:
    executor = _executor()

    assert executor._should_retry_selector_error(ValueError("Element is detached from DOM")) is True
    assert executor._should_retry_selector_error(ValueError("stale element reference")) is True


def test_click_memory_candidates_exclude_form_fields_for_button_like_targets() -> None:
    executor = _executor()
    memory = InMemorySelectorMemoryStore()
    memory.remember_success(
        "test.vitaone.io",
        "click",
        "button:has-text('English')",
        "xpath=//input[@placeholder='Enter email']",
    )
    memory.remember_success(
        "test.vitaone.io",
        "click",
        "button:has-text('English')",
        "button:has-text('English')",
    )
    executor._selector_memory = memory

    candidates = executor._memory_candidates(
        "test.vitaone.io",
        "click",
        "button:has-text('English')",
    )

    assert "xpath=//input[@placeholder='Enter email']" not in candidates
    assert candidates[0] == "button:has-text('English')"


def test_click_memory_candidates_exclude_search_input_for_drag_field_alias() -> None:
    executor = _executor()
    memory = InMemorySelectorMemoryStore()
    memory.remember_success(
        "test.vitaone.io",
        "click",
        "{{selector.short_answer_source}}",
        "input[placeholder='Search']",
    )
    memory.remember_success(
        "test.vitaone.io",
        "click",
        "{{selector.short_answer_source}}",
        "[data-testid='field-short-answer']",
    )
    executor._selector_memory = memory

    candidates = executor._memory_candidates(
        "test.vitaone.io",
        "click",
        "{{selector.short_answer_source}}",
    )

    assert "input[placeholder='Search']" not in candidates
    assert "[data-testid='field-short-answer']" in candidates


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


def test_transition_label_candidates_include_common_prompt_typo_variant() -> None:
    executor = _executor()

    candidates = executor._selector_candidates(
        raw_selector="{{selector.transition_canvas_label}}",
        step_type="click",
        selector_profile={},
        test_data={"NOW_YYYYMMDD_HHMMSS": "20260320_111947"},
        run_domain=None,
        text_hint="Tranisition_{{NOW_YYYYMMDD_HHMMSS}}",
    )

    assert "text=Tranisition_20260320_111947" in candidates
    assert "text=Transition_20260320_111947" in candidates


def test_login_click_timeout_fails_instead_of_claiming_post_login_success() -> None:
    executor = _executor(step_timeout_seconds=4)

    class _Browser:
        async def click(self, selector: str) -> str:
            raise TimeoutError("TimeoutError()")

        async def wait_for(
            self,
            *,
            until: str,
            ms: int,
            selector: str | None = None,
            load_state: str | None = None,
        ) -> str:
            if selector == "button#createForm":
                return "visible"
            raise TimeoutError(f"Missing {selector}")

    executor._browser = _Browser()

    with pytest.raises(ValueError, match="All selector candidates failed"):
        asyncio.run(
            executor._dispatch_step(
                SimpleNamespace(test_data={}, selector_profile={}, start_url=None, steps=[]),
                {
                    "type": "click",
                    "selector": "{{selector.login_button}}",
                },
            )
        )


def test_login_click_hang_fails_instead_of_claiming_post_login_success() -> None:
    executor = _executor(step_timeout_seconds=6)

    class _Browser:
        async def click(self, selector: str) -> str:
            await asyncio.sleep(3.5)
            return f"Clicked {selector}"

        async def wait_for(
            self,
            *,
            until: str,
            ms: int,
            selector: str | None = None,
            load_state: str | None = None,
        ) -> str:
            if selector == "button#createForm":
                return "visible"
            raise TimeoutError(f"Missing {selector}")

    executor._browser = _Browser()

    with pytest.raises((ValueError, TimeoutError)):
        asyncio.run(
            executor._dispatch_step(
                SimpleNamespace(test_data={}, selector_profile={}, start_url=None, steps=[]),
                {
                    "type": "click",
                    "selector": "{{selector.login_button}}",
                },
            )
        )


def test_grounded_selection_stops_on_wrong_page_before_profile_guessing() -> None:
    executor = _executor(step_timeout_seconds=4)
    call_count = 0

    class _Browser:
        async def inspect_page(self, include_screenshot: bool = True) -> dict[str, object]:
            return {
                "url": "https://example.com/login",
                "title": "Login",
                "text_excerpt": "Sign in to continue",
                "interactive_elements": [
                    {
                        "tag": "input",
                        "role": "textbox",
                        "name": "username",
                        "id": "username",
                        "visible": True,
                        "enabled": True,
                        "scope": "form",
                    },
                    {
                        "tag": "button",
                        "role": "button",
                        "text": "Sign In",
                        "name": "login",
                        "id": "login",
                        "visible": True,
                        "enabled": True,
                        "scope": "form",
                    },
                ],
                "page_count": 1,
            }

    executor._browser = _Browser()

    async def operation(selector: str) -> str:
        nonlocal call_count
        call_count += 1
        return f"Clicked {selector}"

    with pytest.raises(ValueError, match="Grounded selection failed"):
        asyncio.run(
            executor._run_with_selector_fallback(
                raw_selector="{{selector.create_form}}",
                step_type="click",
                selector_profile={},
                test_data={},
                run_domain="test.vitaone.io",
                operation=operation,
            )
        )

    assert call_count == 0


def test_grounded_selection_prefers_live_page_match_over_profile_guess() -> None:
    executor = _executor(step_timeout_seconds=4)

    class _Browser:
        async def inspect_page(self, include_screenshot: bool = True) -> dict[str, object]:
            return {
                "url": "https://example.com/forms",
                "title": "Forms",
                "text_excerpt": "Create Form",
                "interactive_elements": [
                    {
                        "tag": "button",
                        "role": "button",
                        "text": "Create Form",
                        "name": "createFormLive",
                        "id": "create-form-live",
                        "visible": True,
                        "enabled": True,
                        "scope": "main",
                    }
                ],
                "page_count": 1,
            }

    executor._browser = _Browser()

    result = asyncio.run(
        executor._run_with_selector_fallback(
            raw_selector="{{selector.create_form}}",
            step_type="click",
            selector_profile={},
            test_data={},
            run_domain="test.vitaone.io",
            operation=lambda selector: asyncio.sleep(0, result=f"Clicked {selector}"),
        )
    )

    assert result == "Clicked #create-form-live"


def test_grounded_selection_prefers_matching_live_button_over_nav_links() -> None:
    executor = _executor(step_timeout_seconds=4)

    class _Browser:
        async def inspect_page(self, include_screenshot: bool = True) -> dict[str, object]:
            return {
                "url": "https://example.com/forms",
                "title": "Forms",
                "text_excerpt": "Create Form Lists Custom fields Content blocks",
                "interactive_elements": [
                    {
                        "tag": "a",
                        "role": "link",
                        "text": "Lists",
                        "href": "/admin-next/engage/forms/lists",
                        "visible": True,
                        "enabled": True,
                        "scope": "main",
                    },
                    {
                        "tag": "a",
                        "role": "link",
                        "text": "Custom fields",
                        "href": "/admin-next/engage/forms/fields",
                        "visible": True,
                        "enabled": True,
                        "scope": "main",
                    },
                    {
                        "tag": "button",
                        "role": "button",
                        "text": "Create Form",
                        "name": "createForm",
                        "id": "create-form-live",
                        "visible": True,
                        "enabled": True,
                        "scope": "main",
                    },
                ],
                "page_count": 1,
            }

        async def wait_for(
            self,
            *,
            until: str,
            ms: int,
            selector: str | None = None,
            load_state: str | None = None,
        ) -> str:
            return "visible"

    executor._browser = _Browser()

    result = asyncio.run(
        executor._run_with_selector_fallback(
            raw_selector="{{selector.create_form}}",
            step_type="click",
            selector_profile={},
            test_data={},
            run_domain="test.vitaone.io",
            operation=lambda selector: asyncio.sleep(0, result=f"Clicked {selector}"),
        )
    )

    assert result == "Clicked #create-form-live"


def test_validate_click_post_action_rejects_wrong_target_even_if_navigation_changes() -> None:
    executor = _executor()

    class _Browser:
        async def assess_click_effect(
            self,
            selector: str,
            before_snapshot: dict[str, object] | None = None,
            raw_selector: str | None = None,
            text_hint: str | None = None,
            target_context: dict[str, object] | None = None,
        ) -> dict[str, str]:
            return {
                "status": "passed",
                "detail": "URL changed from /forms to /forms/lists",
            }

    executor._browser = _Browser()

    with pytest.raises(ValueError, match="Click target mismatch"):
        asyncio.run(
            executor._validate_click_post_action(
                resolved_selector='a[href*="/admin-next/engage/forms/lists"]',
                raw_selector="{{selector.create_form}}",
                text_hint=None,
                before_snapshot={
                    "page_snapshot": {
                        "url": "https://test.vitaone.io/admin-next/engage/forms",
                        "title": "Forms - Engage",
                    },
                    "element_context": {
                        "text": "Lists",
                        "title": "",
                        "aria": "",
                        "href": "/admin-next/engage/forms/lists",
                    },
                },
            )
        )


def test_validate_click_post_action_accepts_sign_in_for_login_button() -> None:
    executor = _executor()

    class _Browser:
        async def assess_click_effect(
            self,
            selector: str,
            before_snapshot: dict[str, object] | None = None,
            raw_selector: str | None = None,
            text_hint: str | None = None,
            target_context: dict[str, object] | None = None,
        ) -> dict[str, str]:
            return {
                "status": "passed",
                "detail": "URL changed from /auth to /dashboard",
            }

    executor._browser = _Browser()

    result = asyncio.run(
        executor._validate_click_post_action(
            resolved_selector='button:has-text("Sign In")',
            raw_selector="{{selector.login_button}}",
            text_hint=None,
            before_snapshot={
                "page_snapshot": {
                    "url": "https://example.com/auth",
                    "title": "Sign in",
                },
                "element_context": {
                    "text": "Sign In",
                    "title": "",
                    "aria": "",
                    "href": "",
                    "selector": 'button:has-text("Sign In")',
                },
            },
        )
    )

    assert result == "post_validation=passed (URL changed from /auth to /dashboard)"


def test_transition_canvas_click_short_circuits_when_label_is_visible() -> None:
    executor = _executor(step_timeout_seconds=6)

    class _Browser:
        async def wait_for(
            self,
            *,
            until: str,
            ms: int,
            selector: str | None = None,
            load_state: str | None = None,
        ) -> str:
            if selector == "text=Transition_20260320_123745":
                return "visible"
            raise TimeoutError(f"Missing {selector}")

        async def click(self, selector: str) -> str:
            raise AssertionError("click should not be attempted when transition label is already visible")

    executor._browser = _Browser()

    result = asyncio.run(
        executor._dispatch_step(
            SimpleNamespace(
                test_data={"NOW_YYYYMMDD_HHMMSS": "20260320_123745"},
                selector_profile={},
                start_url=None,
                steps=[],
            ),
            {
                "type": "click",
                "selector": "{{selector.transition_canvas_label}}",
                "text_hint": "Tranisition_{{NOW_YYYYMMDD_HHMMSS}}",
            },
        )
    )

    assert result == "Transition label is visible on canvas"


def test_transition_canvas_click_becomes_non_blocking_when_editor_is_visible() -> None:
    executor = _executor(step_timeout_seconds=6)

    class _Browser:
        async def wait_for(
            self,
            *,
            until: str,
            ms: int,
            selector: str | None = None,
            load_state: str | None = None,
        ) -> str:
            if selector == "button:has-text('Save Changes')":
                return "visible"
            raise TimeoutError(f"Missing {selector}")

        async def click(self, selector: str) -> str:
            raise AssertionError("click should not be attempted when transition click is treated as non-blocking")

    executor._browser = _Browser()

    result = asyncio.run(
        executor._dispatch_step(
            SimpleNamespace(
                test_data={"NOW_YYYYMMDD_HHMMSS": "20260320_155646"},
                selector_profile={},
                start_url=None,
                steps=[],
            ),
            {
                "type": "click",
                "selector": "{{selector.transition_canvas_label}}",
                "text_hint": "Tranisition_{{NOW_YYYYMMDD_HHMMSS}}",
            },
        )
    )

    assert result == "Transition canvas click treated as non-blocking"


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
