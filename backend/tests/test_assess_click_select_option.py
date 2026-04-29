from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp.browser_client import PlaywrightBrowserMCPClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client() -> PlaywrightBrowserMCPClient:
    client = PlaywrightBrowserMCPClient.__new__(PlaywrightBrowserMCPClient)
    return client


def _make_before_snapshot(url: str = "https://example.com") -> dict[str, Any]:
    return {
        "url": url,
        "title": "Test Page",
        "text_excerpt": "some text",
        "page_count": 1,
    }


async def _run_assess(
    client: PlaywrightBrowserMCPClient,
    tag: str,
    *,
    before_snapshot: dict[str, Any] | None = None,
    target_context: dict[str, Any] | None = None,
    locator_visible: bool = True,
    locator_enabled: bool = True,
    evaluate_side_effect: list | None = None,
) -> dict[str, Any]:
    """
    Patch the Playwright locator and page, then call assess_click_effect.
    evaluate_side_effect: ordered list of return values for locator.evaluate() calls.
    """
    before = before_snapshot or _make_before_snapshot()

    mock_locator = AsyncMock()
    mock_locator.is_visible = AsyncMock(return_value=locator_visible)
    mock_locator.is_enabled = AsyncMock(return_value=locator_enabled)

    eval_returns = list(evaluate_side_effect or [tag])
    mock_locator.evaluate = AsyncMock(side_effect=eval_returns)

    mock_page = AsyncMock()
    mock_page.wait_for_url = AsyncMock(side_effect=Exception("no nav"))
    mock_page.wait_for_timeout = AsyncMock()
    mock_page.locator = MagicMock(return_value=MagicMock(first=mock_locator))

    mock_context = SimpleNamespace(page=mock_page)
    client._active_context = MagicMock(return_value=mock_context)

    # after_snapshot returns the same URL (no navigation)
    async def _inspect(*a, **kw):
        return before.copy()

    client.inspect_page = AsyncMock(side_effect=_inspect)

    return await client.assess_click_effect(
        selector="dummy",
        before_snapshot=before,
        target_context=target_context,
    )


# ---------------------------------------------------------------------------
# <select> — always pass
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_select_click_passes_unconditionally():
    client = _make_client()
    result = await _run_assess(client, tag="select", evaluate_side_effect=["select"])
    assert result["status"] == "passed"
    assert "dropdown" in result["detail"].lower()


@pytest.mark.asyncio
async def test_select_click_passes_even_when_enabled_visible():
    """select stays visible+enabled after being clicked — should still pass."""
    client = _make_client()
    result = await _run_assess(
        client, tag="select",
        locator_visible=True, locator_enabled=True,
        evaluate_side_effect=["select"],
    )
    assert result["status"] == "passed"


# ---------------------------------------------------------------------------
# <option> — strict pre/post comparison
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_option_passes_when_parent_value_changed():
    """Pre-state available and parent select value changed → pass."""
    client = _make_client()
    result = await _run_assess(
        client,
        tag="option",
        target_context={"parent_select_value": "nike"},   # pre-click: nike selected
        evaluate_side_effect=[
            "option",                # tagName eval
            "adidas",               # post: parentElement.value
        ],
    )
    assert result["status"] == "passed"
    assert "parent select value changed" in result["detail"].lower()


@pytest.mark.asyncio
async def test_option_fails_when_parent_value_unchanged():
    """Pre-state available but value did not change (already selected) → fail."""
    client = _make_client()
    result = await _run_assess(
        client,
        tag="option",
        target_context={"parent_select_value": "nike"},   # pre-click: nike
        evaluate_side_effect=[
            "option",
            "nike",                 # post: still nike — nothing changed
        ],
    )
    assert result["status"] == "failed"
    assert "already been selected" in result["detail"].lower()


# ---------------------------------------------------------------------------
# <option> — fallback when no pre-state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_option_fallback_passes_when_option_is_now_active():
    """No pre-state, but option.value == parent.value after click → pass."""
    client = _make_client()
    result = await _run_assess(
        client,
        tag="option",
        target_context=None,        # no pre-click state
        evaluate_side_effect=[
            "option",               # tagName
            "adidas",               # post: parent.value
            "adidas",               # option.value  (matches → active)
        ],
    )
    assert result["status"] == "passed"
    assert "no pre-state" in result["detail"].lower()


@pytest.mark.asyncio
async def test_option_fallback_fails_when_option_not_active():
    """No pre-state, and option.value != parent.value → fail."""
    client = _make_client()
    result = await _run_assess(
        client,
        tag="option",
        target_context=None,
        evaluate_side_effect=[
            "option",
            "nike",                 # post: parent.value is nike
            "adidas",               # option.value is adidas — mismatch
        ],
    )
    assert result["status"] == "failed"
    assert "not result in the option being selected" in result["detail"].lower()


# ---------------------------------------------------------------------------
# Regressions — input / textarea behaviour unchanged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_input_click_still_passes():
    client = _make_client()
    result = await _run_assess(client, tag="input", evaluate_side_effect=["input"])
    assert result["status"] == "passed"
    assert "focus" in result["detail"].lower() or "activation" in result["detail"].lower()


@pytest.mark.asyncio
async def test_textarea_click_still_passes():
    client = _make_client()
    result = await _run_assess(client, tag="textarea", evaluate_side_effect=["textarea"])
    assert result["status"] == "passed"
