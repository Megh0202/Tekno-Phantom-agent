"""
Verification tests for Steps 5, 6, and 7:
  - Step 5: Fast-fail probe (_probe_element_present + candidate loop skipping)
  - Step 6: DOM Signature Memory (remember/recall + find_by_signatures)
  - Step 7: Pre-action page health check (_check_page_health)
"""
from __future__ import annotations

import asyncio
import tempfile
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.runtime.executor import AgentExecutor
from app.runtime.perception import (
    ElementIndex,
    IndexedElement,
    find_by_signatures,
)
from app.runtime.selector_memory import (
    InMemorySelectorMemoryStore,
    SqliteSelectorMemoryStore,
)
from app.schemas import RunState, RunStatus, StepRuntimeState, StepStatus


# ---------------------------------------------------------------------------
# Helpers shared across all test groups
# ---------------------------------------------------------------------------

def _make_executor() -> AgentExecutor:
    executor = AgentExecutor.__new__(AgentExecutor)
    executor._settings = SimpleNamespace(
        step_timeout_seconds=10,
        selector_recovery_enabled=True,
        selector_recovery_attempts=1,
        selector_recovery_delay_ms=0,
        execution_fast_path_enabled=True,
        execution_fast_path_action_timeout_seconds=4,
        execution_fast_path_selector_timeout_ms=2000,
    )
    executor._selector_memory = None
    executor._step_trace_context = ContextVar("step_trace_ctx", default=None)
    executor._step_state_context = ContextVar("step_state_ctx", default=None)
    return executor


def _make_run(start_url: str = "https://example.com") -> RunState:
    return RunState(
        run_name="test-run",
        prompt="test",
        start_url=start_url,
        steps=[],
    )


def _make_step(step_type: str = "click", selector: str = "button") -> StepRuntimeState:
    return StepRuntimeState(index=0, type=step_type, input={"selector": selector})


def _make_element(
    *,
    tag: str = "button",
    role: str = "button",
    text: str = "",
    aria: str = "",
    name: str = "",
    el_id: str = "",
    testid: str = "",
    placeholder: str = "",
    selectors: tuple[str, ...] = (),
) -> IndexedElement:
    return IndexedElement(
        tag=tag, role=role, text=text, aria=aria, name=name,
        el_id=el_id, testid=testid, placeholder=placeholder,
        title="", el_type="", visible=True, enabled=True,
        selectors=selectors,
    )


# ---------------------------------------------------------------------------
# Step 5: Fast-fail probe
# ---------------------------------------------------------------------------

class _ProbePassBrowser:
    """Browser stub where wait_for always succeeds (element is present)."""
    async def wait_for(self, until: str, ms: int = 0, selector: str = "", load_state=None) -> str:
        return "ok"


class _ProbeFailBrowser:
    """Browser stub where wait_for always raises TimeoutError (element absent)."""
    async def wait_for(self, until: str, ms: int = 0, selector: str = "", load_state=None) -> str:
        raise asyncio.TimeoutError("probe timeout")


async def test_probe_returns_true_when_element_present() -> None:
    executor = _make_executor()
    executor._browser = _ProbePassBrowser()
    result = await executor._probe_element_present("#submit", timeout_ms=500)
    assert result is True


async def test_probe_returns_false_when_element_absent() -> None:
    executor = _make_executor()
    executor._browser = _ProbeFailBrowser()
    result = await executor._probe_element_present("#ghost", timeout_ms=100)
    assert result is False


async def test_probe_returns_false_on_generic_exception() -> None:
    """Any exception from the browser (not just TimeoutError) must return False."""
    class _ErrorBrowser:
        async def wait_for(self, **_kwargs):
            raise RuntimeError("unexpected browser error")

    executor = _make_executor()
    executor._browser = _ErrorBrowser()
    result = await executor._probe_element_present(".some-selector", timeout_ms=100)
    assert result is False


# ---------------------------------------------------------------------------
# Step 6a: InMemorySelectorMemoryStore — signature storage and retrieval
# ---------------------------------------------------------------------------

def test_remember_and_get_signature_basic() -> None:
    store = InMemorySelectorMemoryStore()
    sig = {"tag": "button", "role": "button", "text": "Submit", "aria": "", "name": "", "id": "", "testid": "", "placeholder": ""}
    store.remember_signature("example.com", "click", "submit button", sig)
    results = store.get_signatures("example.com", "click", "submit button")
    assert len(results) == 1
    assert results[0]["text"] == "Submit"


def test_signature_score_increments_on_duplicate() -> None:
    store = InMemorySelectorMemoryStore()
    sig = {"tag": "input", "role": "textbox", "text": "", "aria": "Email", "name": "email", "id": "", "testid": "", "placeholder": ""}
    store.remember_signature("app.test", "type", "email input", sig)
    store.remember_signature("app.test", "type", "email input", sig)
    store.remember_signature("app.test", "type", "email input", sig)
    results = store.get_signatures("app.test", "type", "email input")
    # Should have exactly one entry (same sig_json), but score = 3
    assert len(results) == 1


def test_signature_returns_empty_for_unknown_key() -> None:
    store = InMemorySelectorMemoryStore()
    results = store.get_signatures("unknown.com", "click", "nonexistent")
    assert results == []


def test_multiple_different_signatures_returned_in_score_order() -> None:
    store = InMemorySelectorMemoryStore()
    sig_a = {"tag": "button", "role": "button", "text": "Login", "aria": "", "name": "", "id": "", "testid": "", "placeholder": ""}
    sig_b = {"tag": "a", "role": "link", "text": "Login", "aria": "", "name": "", "id": "", "testid": "", "placeholder": ""}
    store.remember_signature("app.test", "click", "login", sig_a)
    store.remember_signature("app.test", "click", "login", sig_a)  # score=2
    store.remember_signature("app.test", "click", "login", sig_b)  # score=1
    results = store.get_signatures("app.test", "click", "login", limit=5)
    # sig_a has higher score so it should come first
    assert results[0]["tag"] == "button"
    assert results[1]["tag"] == "a"


# ---------------------------------------------------------------------------
# Step 6b: SqliteSelectorMemoryStore — signature persistence and reload
# ---------------------------------------------------------------------------

def test_sqlite_signature_persists_across_reload() -> None:
    # ignore_cleanup_errors=True avoids PermissionError on Windows where
    # SQLite keeps a file handle open until GC collects the store object.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = Path(tmpdir) / "mem.db"
        sig = {"tag": "button", "role": "button", "text": "Buy Now", "aria": "", "name": "", "id": "", "testid": "", "placeholder": ""}

        store1 = SqliteSelectorMemoryStore(db_path)
        store1.remember_signature("shop.test", "click", "buy now", sig)
        del store1  # close connections before reloading

        store2 = SqliteSelectorMemoryStore(db_path)
        results = store2.get_signatures("shop.test", "click", "buy now")
        assert len(results) == 1
        assert results[0]["text"] == "Buy Now"


def test_sqlite_signature_upsert_increments_score() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = Path(tmpdir) / "mem.db"
        sig = {"tag": "input", "role": "", "text": "", "aria": "Search", "name": "q", "id": "", "testid": "", "placeholder": ""}
        store = SqliteSelectorMemoryStore(db_path)
        for _ in range(5):
            store.remember_signature("search.test", "type", "search box", sig)
        # Still one unique entry (same signature JSON)
        results = store.get_signatures("search.test", "type", "search box")
        assert len(results) == 1


# ---------------------------------------------------------------------------
# Step 6c: find_by_signatures — matching signatures against live DOM
# ---------------------------------------------------------------------------

def _make_index(*elements: IndexedElement) -> ElementIndex:
    return ElementIndex(url="https://example.com", elements=list(elements))


def test_find_by_signatures_matches_on_id_and_text() -> None:
    el = _make_element(tag="button", role="button", text="Save", el_id="save-btn", selectors=("#save-btn",))
    index = _make_index(el)
    sig = {"tag": "button", "role": "button", "text": "Save", "aria": "", "name": "", "id": "save-btn", "testid": "", "placeholder": ""}
    results = find_by_signatures([sig], index)
    assert "#save-btn" in results


def test_find_by_signatures_matches_on_aria_and_role() -> None:
    el = _make_element(tag="button", role="button", aria="Close dialog", selectors=("[aria-label='Close dialog']",))
    index = _make_index(el)
    sig = {"tag": "button", "role": "button", "text": "", "aria": "Close dialog", "name": "", "id": "", "testid": "", "placeholder": ""}
    results = find_by_signatures([sig], index)
    assert "[aria-label='Close dialog']" in results


def test_find_by_signatures_matches_on_testid() -> None:
    el = _make_element(tag="button", testid="submit-form", selectors=("[data-testid='submit-form']",))
    index = _make_index(el)
    sig = {"tag": "button", "role": "button", "text": "Submit", "aria": "", "name": "", "id": "", "testid": "submit-form", "placeholder": ""}
    results = find_by_signatures([sig], index)
    assert "[data-testid='submit-form']" in results


def test_find_by_signatures_no_match_returns_empty() -> None:
    el = _make_element(tag="button", text="Cancel", selectors=("button:has-text('Cancel')",))
    index = _make_index(el)
    # Signature doesn't match Cancel button at all
    sig = {"tag": "input", "role": "textbox", "text": "Username", "aria": "Username", "name": "username", "id": "", "testid": "", "placeholder": ""}
    results = find_by_signatures([sig], index)
    assert results == []


def test_find_by_signatures_empty_inputs() -> None:
    assert find_by_signatures([], _make_index()) == []
    el = _make_element(tag="button", selectors=("button",))
    assert find_by_signatures([], _make_index(el)) == []


def test_find_by_signatures_deduplicates_results() -> None:
    """Two signatures matching the same element should yield one selector, not two."""
    el = _make_element(tag="button", text="OK", el_id="ok-btn", selectors=("#ok-btn",))
    index = _make_index(el)
    sig1 = {"tag": "button", "role": "button", "text": "OK", "aria": "", "name": "", "id": "ok-btn", "testid": "", "placeholder": ""}
    sig2 = {"tag": "button", "role": "button", "text": "OK", "aria": "", "name": "", "id": "ok-btn", "testid": "", "placeholder": ""}
    results = find_by_signatures([sig1, sig2], index)
    assert results.count("#ok-btn") == 1


# ---------------------------------------------------------------------------
# Step 7: Pre-action page health check
# ---------------------------------------------------------------------------

def _snap(
    url: str = "https://example.com/dashboard",
    title: str = "Dashboard",
    text_excerpt: str = "Welcome to the app",
    interactive_elements: list[dict] | None = None,
) -> dict[str, Any]:
    return {
        "url": url,
        "title": title,
        "text_excerpt": text_excerpt,
        "interactive_elements": interactive_elements if interactive_elements is not None else [
            {"tag": "button", "text": "Save", "visible": True, "role": "button"},
            {"tag": "input", "text": "", "visible": True, "role": "textbox"},
        ],
    }


# --- Error page detection ---

def test_health_check_blocks_on_404_title() -> None:
    snap = _snap(title="404 Not Found", text_excerpt="The page you requested could not be found.")
    result = AgentExecutor._check_page_health(snap, _make_run(), _make_step())
    assert result["status"] == "block"
    assert any(i["type"] == "error_page" for i in result["issues"])


def test_health_check_blocks_on_500_title() -> None:
    snap = _snap(title="500 Internal Server Error")
    result = AgentExecutor._check_page_health(snap, _make_run(), _make_step())
    assert result["status"] == "block"


def test_health_check_blocks_on_forbidden_title() -> None:
    snap = _snap(title="403 Forbidden")
    result = AgentExecutor._check_page_health(snap, _make_run(), _make_step())
    assert result["status"] == "block"


def test_health_check_blocks_on_browser_dns_error() -> None:
    snap = _snap(
        title="example.com refused to connect",
        text_excerpt="err_connection_refused",
        interactive_elements=[],
    )
    result = AgentExecutor._check_page_health(snap, _make_run(), _make_step())
    assert result["status"] == "block"
    assert any(i["type"] == "browser_error" for i in result["issues"])


def test_health_check_blocks_on_nearly_empty_page_with_404_text() -> None:
    # Only 1 visible element + "404" in text_excerpt → should block
    snap = _snap(
        title="Oops",
        text_excerpt="404 The resource you requested was not found.",
        interactive_elements=[{"tag": "a", "text": "Go home", "visible": True, "role": "link"}],
    )
    result = AgentExecutor._check_page_health(snap, _make_run(), _make_step())
    assert result["status"] == "block"


def test_health_check_ok_on_normal_page_containing_404_in_body() -> None:
    # A normal page with many elements that happens to mention "404" somewhere
    # should NOT be flagged as an error page (title is clean).
    elements = [{"tag": "button", "text": f"item {i}", "visible": True, "role": "button"} for i in range(10)]
    snap = _snap(
        title="Product catalogue",
        text_excerpt="We handle 404 errors gracefully in our API",
        interactive_elements=elements,
    )
    result = AgentExecutor._check_page_health(snap, _make_run(), _make_step())
    assert result["status"] == "ok"


# --- Domain mismatch detection ---

def test_health_check_warns_on_domain_mismatch() -> None:
    snap = _snap(url="https://evil.phishing.com/login")
    result = AgentExecutor._check_page_health(snap, _make_run(start_url="https://example.com"), _make_step())
    assert result["status"] == "warn"
    assert any(i["type"] == "domain_mismatch" for i in result["issues"])


def test_health_check_ok_on_same_domain() -> None:
    snap = _snap(url="https://example.com/checkout")
    result = AgentExecutor._check_page_health(snap, _make_run(start_url="https://example.com"), _make_step())
    assert result["status"] == "ok"


def test_health_check_ok_on_subdomain_of_expected() -> None:
    snap = _snap(url="https://app.example.com/dashboard")
    result = AgentExecutor._check_page_health(snap, _make_run(start_url="https://example.com"), _make_step())
    assert result["status"] == "ok"


def test_health_check_ok_on_www_variant() -> None:
    snap = _snap(url="https://www.example.com/page")
    result = AgentExecutor._check_page_health(snap, _make_run(start_url="https://example.com"), _make_step())
    assert result["status"] == "ok"


# --- Modal / overlay detection ---

def test_health_check_warns_on_blocking_dialog() -> None:
    elements = [
        {"tag": "div", "role": "dialog", "text": "Your session has expired. Please log in again.", "visible": True},
        {"tag": "button", "role": "button", "text": "Submit", "visible": True},
    ]
    snap = _snap(interactive_elements=elements)
    result = AgentExecutor._check_page_health(snap, _make_run(), _make_step())
    assert result["status"] == "warn"
    assert any(i["type"] == "modal_overlay" for i in result["issues"])


def test_health_check_does_not_warn_for_cookie_consent_dialog() -> None:
    """Cookie consent dialogs are already handled separately — should not raise modal_overlay."""
    elements = [
        {"tag": "div", "role": "dialog", "text": "We use cookies to improve your experience. Accept all.", "visible": True},
        {"tag": "button", "role": "button", "text": "Accept all", "visible": True},
    ]
    snap = _snap(interactive_elements=elements)
    result = AgentExecutor._check_page_health(snap, _make_run(), _make_step())
    modal_issues = [i for i in result.get("issues", []) if i["type"] == "modal_overlay"]
    assert modal_issues == []


# --- Loading state detection ---

def test_health_check_warns_on_loading_state() -> None:
    snap = _snap(title="loading...", interactive_elements=[])
    result = AgentExecutor._check_page_health(snap, _make_run(), _make_step())
    assert result["status"] == "warn"
    assert any(i["type"] == "loading_state" for i in result["issues"])


# --- OK cases ---

def test_health_check_ok_for_none_snapshot() -> None:
    result = AgentExecutor._check_page_health(None, _make_run(), _make_step())
    assert result["status"] == "ok"
    assert result["issues"] == []


def test_health_check_ok_for_healthy_page() -> None:
    snap = _snap()
    result = AgentExecutor._check_page_health(snap, _make_run(), _make_step())
    assert result["status"] == "ok"
    assert result["issues"] == []
