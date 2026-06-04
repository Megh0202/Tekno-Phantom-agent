"""Human-in-the-loop (HITL) manager.

Owns all mechanics of the human recovery recording session:
  - Starting / reading / stopping the browser-side JS recorder
  - Normalising the raw event stream into a clean interaction sequence
  - Building the human-readable confirmation prompt

Deliberately does NOT touch RunState, StepRuntimeState, or RunStore.
All state-machine transitions stay in executor.py.  This class is
injected into AgentExecutor and called at the appropriate points in the
polling loop and confirmation flow.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.mcp.browser_client import BrowserMCPClient

LOGGER = logging.getLogger("tekno.phantom.hitl")


class HitlManager:
    """Manages browser-side interaction recording during HITL recovery sessions."""

    def __init__(self, browser: BrowserMCPClient) -> None:
        self._browser = browser

    # ------------------------------------------------------------------
    # Recording lifecycle
    # ------------------------------------------------------------------

    async def start_recording(self, run_id: str) -> None:
        """Inject the JS recorder into the live page. Non-fatal on any error."""
        try:
            await self._browser.start_interaction_recording(run_id)
        except Exception as exc:
            LOGGER.debug(
                "Run %s: recovery recorder start failed (non-fatal): %s", run_id, exc
            )

    async def stop_recording(self, run_id: str) -> None:
        """Disable and clear the browser-side recorder. Non-fatal on any error."""
        try:
            await self._browser.stop_interaction_recording(run_id)
        except Exception as exc:
            LOGGER.debug(
                "Run %s: recovery recorder stop failed (non-fatal): %s", run_id, exc
            )

    async def read_interactions(self, run_id: str) -> list[dict]:
        """Read and atomically clear the browser interaction buffer.

        Returns an empty list on any error so the polling loop never crashes.
        """
        try:
            return await self._browser.get_recorded_interactions(run_id)
        except Exception as exc:
            LOGGER.debug(
                "Run %s: get_recorded_interactions failed (non-fatal): %s", run_id, exc
            )
            return []

    # ------------------------------------------------------------------
    # Event normalisation
    # ------------------------------------------------------------------

    @staticmethod
    def normalize(events: list[dict]) -> list[dict]:
        """Convert a raw DOM event stream into a clean, deduplicated sequence.

        Rules:
        - ``input`` events: buffer per (tag, name, type) field key — keep only
          the latest value so only the final typed value is retained.
        - ``change`` events: supersede any pending input for the same field and
          are emitted immediately (they represent the committed, stable value).
        - ``click`` events: flush all pending inputs first (focus moved away),
          then emit the click; consecutive identical clicks within 500 ms are
          deduplicated.
        - Noise: HTML / BODY / DOCUMENT tags, empty-value inputs, and events
          with no useful identity are dropped.
        """
        result: list[dict] = []
        pending_input: dict[tuple[str, str, str], dict] = {}
        last_click_sig: tuple | None = None
        # Gate: set True on the first deliberate click.  Anything buffered before
        # the first click is a carry-over from wherever the browser cursor was
        # sitting when recording started and must be discarded.
        first_click_seen = False

        def _field_key(ev: dict) -> tuple[str, str, str]:
            return (
                (ev.get("tag") or "").upper(),
                ev.get("name") or "",
                (ev.get("type") or "").lower(),
            )

        def _flush_pending(except_key: tuple | None = None) -> None:
            for k in list(pending_input):
                if k != except_key:
                    result.append(pending_input.pop(k))

        for ev in events:
            kind = ev.get("kind", "")
            tag = (ev.get("tag") or "").upper()

            if tag in ("HTML", "BODY", "DOCUMENT"):
                continue

            if kind == "input":
                if not (ev.get("name") or ev.get("label") or ev.get("placeholder")):
                    continue
                if ev.get("value") is None and not ev.get("checked"):
                    continue
                pending_input[_field_key(ev)] = ev

            elif kind == "change":
                key = _field_key(ev)
                pending_input.pop(key, None)
                if first_click_seen:
                    result.append(ev)
                # else: fired on blur before the first intentional click — carry-over

            elif kind == "click":
                if not first_click_seen:
                    # First intentional user action: drop everything accumulated
                    # before this point — it is carry-over from pre-recording state.
                    pending_input.clear()
                    first_click_seen = True
                else:
                    _flush_pending()
                ts = int(ev.get("timestamp") or 0)
                sig = (tag, ev.get("name") or "", (ev.get("text") or "").strip(), ts // 500)
                if sig != last_click_sig:
                    result.append(ev)
                    last_click_sig = sig

            else:
                _flush_pending()
                result.append(ev)

        _flush_pending()
        return result

    # ------------------------------------------------------------------
    # Confirmation prompt
    # ------------------------------------------------------------------

    @staticmethod
    def build_confirm_prompt(interactions: list[dict]) -> str:
        """Build a human-readable confirmation prompt for the detected interactions."""

        def _describe(ev: dict) -> str:
            kind = ev.get("kind", "action")
            label = ev.get("label") or ev.get("placeholder") or ev.get("name")
            text = (ev.get("text") or "").strip()
            tag = ev.get("tag") or "?"
            role = ev.get("role") or ""
            target = label or text or (f"{role} {tag}".strip() if role else tag)
            if kind == "click":
                return f"- clicked {target!r}"
            if kind in {"input", "type"}:
                return f"- typed into {target!r}"
            if kind == "toggle":
                state = "checked" if ev.get("checked") else "unchecked"
                return f"- {state} {target!r}"
            if kind in {"change", "select"}:
                value = ev.get("value") or ""
                return f"- selected {value!r} in {target!r}" if value else f"- changed {target!r}"
            return f"- {kind} on {target!r}"

        lines = [_describe(ev) for ev in interactions[:10]]
        summary = "\n".join(lines) if lines else "(no detail available)"
        return (
            f"Detected {len(interactions)} interaction(s) in the browser:\n{summary}\n\n"
            "Confirm these are the correct actions for this step to continue "
            "and save for future use."
        )
