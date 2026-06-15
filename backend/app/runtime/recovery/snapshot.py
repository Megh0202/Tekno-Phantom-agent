from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from app.schemas import FailureContext, InteractionEvent, RecoveryAction, SuccessSignals


def snapshot_item_summary(item: dict[str, Any]) -> dict[str, Any]:
    raw_sels = item.get("selectors")
    top_sels = [str(s)[:120] for s in raw_sels[:3]] if isinstance(raw_sels, list) else []
    return {
        "tag": str(item.get("tag", "")).strip()[:40],
        "role": str(item.get("role", "")).strip()[:40],
        "semantic_role": str(item.get("semantic_role", "")).strip()[:40],
        "text": str(item.get("text", "")).strip()[:120],
        "aria": str(item.get("aria", "")).strip()[:120],
        "label": str(item.get("label", "")).strip()[:120],
        "placeholder": str(item.get("placeholder", "")).strip()[:80],
        "id": str(item.get("id", "")).strip()[:80],
        "name": str(item.get("name", "")).strip()[:80],
        "testid": str(item.get("testid", "")).strip()[:80],
        "scope": str(item.get("scope", "")).strip()[:40],
        "visible": bool(item.get("visible", True)),
        "enabled": bool(item.get("enabled", True)),
        "selectors": top_sels,
    }


def diff_recovery_snapshots(
    baseline: dict[str, Any],
    current: dict[str, Any],
) -> list[str]:
    """
    Compare two inspect_page() snapshots taken during recovery mode and
    return human-readable descriptions of any meaningful UI state changes.

    Uses only snapshot fields confirmed present in interactive_elements:
    url, title, role, visible.  Does not make additional browser API calls.
    """
    changes: list[str] = []

    # 1. URL change
    before_url = str(baseline.get("url", "")).strip()
    after_url = str(current.get("url", "")).strip()
    if before_url and after_url and before_url != after_url:
        changes.append(f"page URL changed ({before_url!r} → {after_url!r})")

    # 2. Page title change
    before_title = str(baseline.get("title", "")).strip()
    after_title = str(current.get("title", "")).strip()
    if before_title and after_title and before_title != after_title:
        changes.append(f"page title changed ({before_title!r} → {after_title!r})")

    def _get_elements(snap: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            el for el in (snap.get("interactive_elements") or [])
            if isinstance(el, dict)
        ]

    before_els = _get_elements(baseline)
    after_els = _get_elements(current)

    # 3. Dialog / modal appeared or closed
    def _has_dialog(els: list[dict[str, Any]]) -> bool:
        return any(
            el.get("role") in {"dialog", "alertdialog"}
            and el.get("visible", True)
            for el in els
        )

    before_dialog = _has_dialog(before_els)
    after_dialog = _has_dialog(after_els)
    if not before_dialog and after_dialog:
        changes.append("modal/dialog appeared")
    elif before_dialog and not after_dialog:
        changes.append("modal/dialog closed")

    # 4. Visible interactive element count shifted by ≥ 3
    # (catches form submissions, tab changes, drawer open/close, etc.)
    before_visible = sum(1 for el in before_els if el.get("visible", True))
    after_visible = sum(1 for el in after_els if el.get("visible", True))
    delta = after_visible - before_visible
    if abs(delta) >= 3:
        direction = "appeared" if delta > 0 else "disappeared"
        changes.append(
            f"{abs(delta)} interactive elements {direction} "
            f"(visible count: {before_visible} → {after_visible})"
        )

    return changes


def extract_domain(url: str) -> str:
    """Extract normalized domain (netloc) from a URL for recipe lookup."""
    return urlparse(url).netloc.lower() or "unknown"


def extract_element_key(step_type: str, step_input: dict[str, Any]) -> str:
    """Derive a stable element fingerprint from step input for recipe lookup.

    Priority: data-testid → accessible name/label → name attribute →
    step_type:normalized-selector → step_type.
    """
    for field in ("testid", "data_testid", "data-testid"):
        v = step_input.get(field)
        if isinstance(v, str) and v.strip():
            return f"testid:{v.strip()}"
    for field in ("accessible_name", "semantic_name", "aria_label",
                  "aria-label", "label", "text_hint"):
        v = step_input.get(field)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()[:80]
    target = step_input.get("target")
    if isinstance(target, dict):
        for field in ("accessible_name", "semantic_name", "aria_label", "label"):
            v = target.get(field)
            if isinstance(v, str) and v.strip():
                return v.strip().lower()[:80]
    for field in ("name",):
        v = step_input.get(field)
        if isinstance(v, str) and v.strip():
            return f"name:{v.strip()}"
    selector = (
        step_input.get("selector")
        or step_input.get("source_selector")
        or ""
    )
    if selector:
        attr_match = re.search(
            r'\[(?:placeholder|aria-label|name)\*?=[\'"]([^\'"]+)[\'"]',
            str(selector), re.IGNORECASE,
        )
        if attr_match:
            return attr_match.group(1).strip().lower()[:80]
        text_match = re.search(r"has-text\(['\"]([^'\"]+)['\"]", str(selector))
        if text_match:
            return text_match.group(1).strip().lower()[:80]
        normalized = re.sub(r"\[.*?\]|\s+", "", str(selector)).strip()[:60]
        if normalized:
            return f"{step_type}:{normalized}"
    return step_type


def build_failure_context(snapshot: dict[str, Any] | None) -> FailureContext:
    """Build FailureContext from a raw _safe_page_snapshot() dict."""
    if not isinstance(snapshot, dict):
        return FailureContext()
    els = [el for el in (snapshot.get("interactive_elements") or [])
           if isinstance(el, dict)]
    has_dialog = any(
        el.get("role") in {"dialog", "alertdialog"} and el.get("visible", True)
        for el in els
    )
    visible_count = sum(1 for el in els if el.get("visible", True))
    return FailureContext(
        url_path=urlparse(str(snapshot.get("url", ""))).path,
        page_title=str(snapshot.get("title", ""))[:160],
        has_dialog=has_dialog,
        interactive_count=visible_count,
    )


def build_failure_context_from_summary(
    summary: dict[str, Any] | None,
) -> FailureContext:
    """Build FailureContext from a _summarize_page_state() dict."""
    if not isinstance(summary, dict):
        return FailureContext()
    sample = summary.get("interactive_sample") or []
    has_dialog = any(
        isinstance(item, dict)
        and item.get("role") in {"dialog", "alertdialog"}
        and item.get("visible", True)
        for item in sample
    )
    return FailureContext(
        url_path=urlparse(str(summary.get("url", ""))).path,
        page_title=str(summary.get("title", ""))[:160],
        has_dialog=has_dialog,
        interactive_count=int(summary.get("visible_interactive_count") or 0),
    )


def derive_success_signals(
    failure_ctx: FailureContext,
    after_snapshot: dict[str, Any] | None,
) -> SuccessSignals:
    """Derive SuccessSignals by comparing failure state with post-recovery page state."""
    if not isinstance(after_snapshot, dict):
        return SuccessSignals()
    after_els = [el for el in (after_snapshot.get("interactive_elements") or [])
                 if isinstance(el, dict)]
    after_has_dialog = any(
        el.get("role") in {"dialog", "alertdialog"} and el.get("visible", True)
        for el in after_els
    )
    after_url_path = urlparse(str(after_snapshot.get("url", ""))).path
    url_changed = after_url_path != failure_ctx.url_path
    return SuccessSignals(
        url_path_changed=url_changed,
        url_path_after=after_url_path if url_changed else None,
        dialog_dismissed=failure_ctx.has_dialog and not after_has_dialog,
        dialog_appeared=not failure_ctx.has_dialog and after_has_dialog,
    )


def promote_interactions_to_actions(
    interactions: list[InteractionEvent],
) -> list[RecoveryAction]:
    """Promote captured InteractionEvents to semantic RecoveryActions for recipe storage."""
    result: list[RecoveryAction] = []
    for ev in interactions:
        kind = ev.kind
        tag = (ev.tag or "").upper()
        role = (ev.role or "").lower()

        # Skip noise: copyright / footer text captured by the JS recorder.
        _display = (ev.label or ev.text or "")
        if "©" in _display or "all rights reserved" in _display.lower():
            continue

        if kind == "navigate":
            action_type = "navigate_to"
            value = ev.url or ev.value
        elif kind in {"input", "type"}:
            action_type = "fill_field"
            value = ev.value
        elif kind in {"change", "select"}:
            action_type = "select_option" if tag == "SELECT" else "fill_field"
            value = ev.value
        elif kind == "toggle":
            action_type = "click_control"
            value = None
        elif kind == "click":
            action_type = (
                "dismiss_dialog" if role in {"dialog", "alertdialog"}
                else "click_control"
            )
            value = None
        else:
            action_type = "click_control"
            value = None

        result.append(RecoveryAction(
            action_type=action_type,
            label=ev.label,
            label_src=ev.label_src,
            role=ev.role,
            text=ev.text,
            placeholder=ev.placeholder,
            option_text=ev.option_text,
            tag=ev.tag,
            name=ev.name,
            selector_chain=ev.selector_chain or [],
            value=value,
            checked=ev.checked,
            clear_first=ev.clear_first,
        ))
    return result
