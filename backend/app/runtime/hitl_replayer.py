"""HitlReplayer — deterministic semantic replay engine for RecoveryRecipe execution.

Reproduces human recovery INTENT, not raw browser events.

Replay strategy for each RecoveryAction (in priority order):
  1. get_by_test_id()       — data-testid / data-cy / data-qa (developer-set, most stable)
  2. get_by_label(label)    — accessible label from any source (app-agnostic)
  3. get_by_role(role, ...) — ARIA role + accessible name
  4. get_by_placeholder()   — input placeholder text
  5. get_by_text()          — visible text (buttons / links)
  6. selector_chain         — CSS fallback anchors, tried in priority order

Action execution:
  fill_field     → locator.fill(value)
  select_option  → select_option(label=option_text) then select_option(value=value)
  click_control  → locator.click()
  dismiss_dialog → locator.click() if found, else Escape key
  navigate_to    → page.goto(value)

Outcome evaluation (SuccessSignals):
  succeeded  — all observed signals matched → auto_recovered
  uncertain  — no signals recorded to validate against → HITL fallback
  failed     — one or more signals clearly did not match → HITL fallback

No AI reasoning. No XPath. No coordinates. No screenshot comparison.
No retry loops. One attempt per locator strategy.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

from app.schemas import FailureContext, RecoveryAction, RecoveryRecipe, SuccessSignals

LOGGER = logging.getLogger("tekno.phantom.replayer")

# Extracts testid value from strings like [data-testid="foo"] or [data-cy='bar']
_TESTID_RE = re.compile(
    r'\[data-(?:testid|cy|qa)\s*=\s*["\']([^"\']+)["\']\]'
)

ReplayOutcome = Literal["succeeded", "uncertain", "failed"]


@dataclass
class ReplayResult:
    """Result of a single recipe replay attempt."""

    outcome: ReplayOutcome
    actions_executed: int                      # actions attempted (locator found)
    actions_total: int                         # total actions in the recipe
    matched_signals: list[str] = field(default_factory=list)
    reason: str = ""


class HitlReplayer:
    """Deterministic semantic replay engine.

    Instantiate once (e.g. inside AgentExecutor) and call try_replay() per attempt.
    The Playwright page is passed at replay time — not at construction — because
    each run owns its own browser context.
    """

    # Short timeout per locator strategy attempt.  Keeps total replay time bounded.
    # With ≤10 actions × 6 strategies × 3 s each worst case = 180 s absolute max,
    # but in practice the first matching strategy returns immediately.
    _LOCATE_TIMEOUT_MS: int = 3_000

    # Timeout for the actual interaction (fill, click, select_option, goto).
    _ACTION_TIMEOUT_MS: int = 5_000

    # Brief pause between actions to let the page settle (React state updates,
    # animations, lazy-loaded option panels).  Not a retry — executed exactly once.
    _BETWEEN_ACTIONS_MS: int = 300

    # ------------------------------------------------------------------ #
    # Public entry point                                                   #
    # ------------------------------------------------------------------ #

    async def try_replay(
        self,
        page: Any,
        recipe: RecoveryRecipe,
    ) -> ReplayResult:
        """Replay all actions in the recipe and evaluate outcome against SuccessSignals.

        Args:
            page:   Live Playwright page for the run (from browser_client.get_live_page()).
            recipe: The RecoveryRecipe to replay.

        Returns:
            ReplayResult with outcome "succeeded" | "uncertain" | "failed".
        """
        LOGGER.info(
            "Replayer: starting replay domain=%r step_type=%r element_key=%r actions=%d",
            recipe.domain, recipe.step_type, recipe.element_key, len(recipe.actions),
        )

        before_snapshot = await self._page_snapshot(page)
        executed = 0

        for i, action in enumerate(recipe.actions):
            LOGGER.debug(
                "Replayer: action %d/%d type=%r label=%r role=%r",
                i + 1, len(recipe.actions),
                action.action_type, action.label, action.role,
            )
            try:
                locator = await self._locate(page, action)
                success = await self._execute_action(page, locator, action)
                if success:
                    executed += 1
                    LOGGER.debug("Replayer: action %d executed", i + 1)
                else:
                    LOGGER.debug("Replayer: action %d skipped (no target found)", i + 1)
            except Exception as exc:
                LOGGER.debug("Replayer: action %d raised (non-fatal): %s", i + 1, exc)

            # Allow the page to settle between actions — not a retry.
            if i < len(recipe.actions) - 1:
                try:
                    await page.wait_for_timeout(self._BETWEEN_ACTIONS_MS)
                except Exception:
                    pass

        after_snapshot = await self._page_snapshot(page)
        outcome, matched, reason = self._evaluate_signals(
            recipe.success_signals, before_snapshot, after_snapshot
        )

        result = ReplayResult(
            outcome=outcome,
            actions_executed=executed,
            actions_total=len(recipe.actions),
            matched_signals=matched,
            reason=reason,
        )
        LOGGER.info(
            "Replayer: outcome=%r executed=%d/%d signals=%r reason=%r",
            outcome, executed, len(recipe.actions), matched, reason,
        )
        return result

    # ------------------------------------------------------------------ #
    # Element location — layered semantic strategy                        #
    # ------------------------------------------------------------------ #

    async def _locate(
        self, page: Any, action: RecoveryAction
    ) -> Any | None:
        """Try each semantic and structural strategy in priority order.

        Returns the first visible Playwright locator found, or None if all fail.
        Each strategy is attempted exactly once — no retries.
        """

        # Strategy 1 — data-testid / data-cy / data-qa
        # Extracted from the first entry of selector_chain if it matches.
        # Developer-set test attributes are the most stable anchor.
        if action.selector_chain:
            m = _TESTID_RE.search(action.selector_chain[0])
            if m:
                result = await self._try_locator(
                    page.get_by_test_id(m.group(1))
                )
                if result is not None:
                    return result

        # Strategy 2 — get_by_label
        # Handles all label-association patterns (aria-label, label[for],
        # wrapping label, visual sibling) without any app-specific knowledge.
        if action.label:
            result = await self._try_locator(page.get_by_label(action.label))
            if result is not None:
                return result

        # Strategy 3 — get_by_role with accessible name
        if action.role:
            name = action.label or action.text or None
            try:
                locator = (
                    page.get_by_role(action.role, name=name)
                    if name
                    else page.get_by_role(action.role)
                )
                result = await self._try_locator(locator)
                if result is not None:
                    return result
            except Exception:
                pass

        # Strategy 4 — get_by_placeholder
        if action.placeholder:
            result = await self._try_locator(
                page.get_by_placeholder(action.placeholder)
            )
            if result is not None:
                return result

        # Strategy 5 — get_by_text (buttons, links, menu items)
        # exact=False allows partial match which is more resilient to minor text changes.
        if action.text:
            result = await self._try_locator(
                page.get_by_text(action.text, exact=False)
            )
            if result is not None:
                return result

        # Strategy 6 — CSS selector_chain (ordered by confidence)
        for selector in action.selector_chain:
            try:
                result = await self._try_locator(page.locator(selector))
                if result is not None:
                    return result
            except Exception:
                continue

        return None

    async def _try_locator(self, locator: Any) -> Any | None:
        """Return locator.first if at least one visible element matches, else None.

        Uses wait_for(state="visible") with a short timeout so failed strategies
        return quickly without hanging the whole replay.
        """
        try:
            first = locator.first
            await first.wait_for(state="visible", timeout=self._LOCATE_TIMEOUT_MS)
            return first
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # Action execution                                                     #
    # ------------------------------------------------------------------ #

    async def _execute_action(
        self, page: Any, locator: Any | None, action: RecoveryAction
    ) -> bool:
        """Execute a single RecoveryAction.

        Returns True if the action was executed, False if it was skipped.
        Raises on unexpected errors so try_replay() can log them non-fatally.
        """
        kind = action.action_type

        if kind == "navigate_to":
            # No locator needed — navigate directly.
            if not action.value:
                LOGGER.debug("Replayer: navigate_to skipped — no URL in action.value")
                return False
            await page.goto(
                action.value,
                wait_until="domcontentloaded",
                timeout=self._ACTION_TIMEOUT_MS,
            )
            return True

        if kind == "dismiss_dialog":
            # Try the recorded close button first; fall back to Escape key.
            if locator is not None:
                await locator.click(timeout=self._ACTION_TIMEOUT_MS)
            else:
                await page.keyboard.press("Escape")
            return True

        # All remaining kinds require a located element.
        if locator is None:
            return False

        if kind == "fill_field":
            # locator.fill() clears existing content then types new value.
            # This is the correct behaviour for clear_first=True (always true for text inputs).
            await locator.fill(action.value or "", timeout=self._ACTION_TIMEOUT_MS)
            return True

        if kind == "select_option":
            # Try visible option label first (more stable than DB value),
            # then fall back to the raw value attribute.
            if action.option_text:
                try:
                    await locator.select_option(
                        label=action.option_text, timeout=self._ACTION_TIMEOUT_MS
                    )
                    return True
                except Exception:
                    pass
            if action.value:
                await locator.select_option(
                    value=action.value, timeout=self._ACTION_TIMEOUT_MS
                )
                return True
            LOGGER.debug("Replayer: select_option skipped — no option_text or value")
            return False

        if kind == "click_control":
            await locator.click(timeout=self._ACTION_TIMEOUT_MS)
            return True

        LOGGER.debug("Replayer: unknown action_type=%r — skipped", kind)
        return False

    # ------------------------------------------------------------------ #
    # Page snapshot                                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def _page_snapshot(page: Any) -> dict:
        """Take a lightweight page snapshot for SuccessSignals evaluation.

        Captures only what can be measured deterministically without
        app-specific knowledge: URL path and dialog presence.
        """
        snapshot: dict = {
            "url_path": "",
            "has_dialog": False,
        }
        try:
            raw_url = page.url or ""
            snapshot["url_path"] = urlparse(raw_url).path
        except Exception:
            pass
        try:
            # Detect any visible ARIA dialog/modal — works across all app frameworks.
            dialog_count = await page.locator(
                "[role='dialog']:visible, dialog:visible"
            ).count()
            snapshot["has_dialog"] = dialog_count > 0
        except Exception:
            pass
        return snapshot

    # ------------------------------------------------------------------ #
    # SuccessSignals evaluation                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _evaluate_signals(
        signals: SuccessSignals,
        before: dict,
        after: dict,
    ) -> tuple[ReplayOutcome, list[str], str]:
        """Compare before/after page snapshots against recorded SuccessSignals.

        Rules:
          - Only evaluate signals that were actually recorded (non-default values).
          - A signal is "observed" if its field was populated at recipe-save time.
          - If no signals were recorded → uncertain (cannot validate).
          - If all observed signals match → succeeded.
          - If any observed signal does not match → failed.

        Returns: (outcome, list_of_matched_signal_names, reason_string)
        """
        observed: list[tuple[str, bool]] = []   # (signal_name, did_it_match)

        # --- URL path transition ---
        if signals.url_path_changed:
            if signals.url_path_after:
                # Exact expected path recorded — check it
                matched = after["url_path"] == signals.url_path_after
            else:
                # Just know it should have changed
                matched = after["url_path"] != before.get("url_path", "")
            observed.append(("url_path_changed", matched))

        # --- Dialog dismissed ---
        if signals.dialog_dismissed:
            # Dialog was open before, should be gone after
            matched = not after["has_dialog"]
            observed.append(("dialog_dismissed", matched))

        # --- Dialog appeared ---
        if signals.dialog_appeared:
            # Dialog was absent before, should be open after
            matched = after["has_dialog"]
            observed.append(("dialog_appeared", matched))

        # --- No signals were recorded at all ---
        if not observed:
            return (
                "uncertain",
                [],
                "no success signals recorded — cannot validate replay outcome",
            )

        matched_names = [name for name, ok in observed if ok]
        failed_names = [name for name, ok in observed if not ok]

        if failed_names:
            return (
                "failed",
                matched_names,
                f"signals not matched: {failed_names}",
            )

        return (
            "succeeded",
            matched_names,
            f"all signals matched: {matched_names}",
        )
