from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.runtime.selectors.utils import extract_selector_text
from app.schemas import StepRuntimeState


@dataclass(frozen=True)
class StepIntent:
    action: str
    element_type: str
    target_text: str | None
    ordinal: int | None
    scope_hint: str | None
    raw_selector: str | None = None
    text_hint: str | None = None


def editable_selector_field(step: StepRuntimeState) -> str | None:
    payload = step.input or {}
    for field in ("selector", "source_selector", "target_selector"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return field
    return None


def step_text_hint(step_input: dict[str, Any], step_type: str = "") -> str | None:
    # Explicit override always wins.
    explicit = step_input.get("text_hint")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    # target.* fields carry element identity (label, visible text, placeholder).
    # Check these before top-level "text"/"value" which for type/select steps
    # hold the content being typed, not the element's name.
    # New canonical fields (accessible_name, semantic_name) take priority over
    # legacy fields (label, text) so plans from the updated planner resolve first.
    target = step_input.get("target")
    if isinstance(target, dict):
        for f in ("accessible_name", "semantic_name", "text", "label", "placeholder", "kind", "role"):
            value = target.get(f)
            if isinstance(value, str) and value.strip():
                return value.strip()

    # For type/select steps top-level "text" and "value" are the content
    # to be typed — they identify the value, not the element.  Returning
    # them as text_hint would corrupt intent (e.g. "user@example.com"
    # instead of "Email").  Skip them for these step types.
    if step_type not in {"type", "select"}:
        for f in ("value", "text"):
            value = step_input.get(f)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return None


def step_context_hint(step_input: dict[str, Any]) -> str | None:
    """Extract the named container section from step input (e.g. 'Login form').
    Reads target.scope (new contract) with fallback to target.context (legacy)."""
    target = step_input.get("target")
    if isinstance(target, dict):
        value = target.get("scope") or target.get("context")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def step_has_semantic_contract(step_input: dict[str, Any]) -> bool:
    """Return True when the step carries at least one v2 semantic target field
    (semantic_name, accessible_name, or expected_role) that drives perception-
    first resolution — as opposed to a bare selector or a legacy target with
    only kind/role/text."""
    target = step_input.get("target")
    if not isinstance(target, dict):
        return False
    return bool(
        target.get("semantic_name")
        or target.get("accessible_name")
        or target.get("expected_role")
    )


def selector_seed_from_target(step_input: dict[str, Any], step_type: str) -> str:
    target = step_input.get("target")
    if not isinstance(target, dict):
        return ""
    for f in ("text", "label", "placeholder", "context"):
        value = target.get(f)
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


def intent_ordinal(value: str) -> int | None:
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


def infer_tag_from_selector(selector: str) -> str:
    """Infer HTML tag from a CSS selector string (e.g. 'input[name=x]' → 'input').
    Returns empty string if no meaningful tag can be inferred."""
    s = selector.strip()
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9]*)(?:[^\w]|$)", s)
    if not m:
        return ""
    tag = m.group(1).lower()
    _MEANINGFUL_TAGS = {"input", "button", "textarea", "select", "a", "label"}
    return tag if tag in _MEANINGFUL_TAGS else ""


def infer_type_from_selector(selector: str) -> str:
    """Infer input[type] from a CSS selector string (e.g. 'input[type=hidden]' → 'hidden')."""
    m = re.search(r"\[type=['\"]?([a-zA-Z]+)['\"]?\]", selector, re.IGNORECASE)
    return m.group(1).lower() if m else ""


def intent_scope_hint(value: str, context_hint: str | None = None) -> str | None:
    # Prefer the plan's explicit target.context (e.g. "Login form", "Search bar")
    # over structural keywords parsed from the selector string.
    if context_hint and context_hint.strip():
        return context_hint.strip().lower()
    lowered = value.lower()
    for scope in ("form", "main", "nav", "article", "header", "footer", "dialog"):
        if scope in lowered:
            return scope
    return None


def intent_target_text(step_type: str, raw_selector: str, text_hint: str | None) -> str | None:
    explicit = extract_selector_text(raw_selector)
    if explicit:
        return explicit

    # Target metadata from the planner is a stronger generic identity signal
    # than selector-derived heuristics — check it before alias expansion or
    # phrase inference so that, e.g., a full multi-word label like
    # "Confirm Password" beats the token "password" extracted from the CSS.
    #
    # Exception: for type/select steps the text_hint may carry the value
    # being typed (e.g. "qa@example.com", "https://…", a generated
    # timestamp) rather than the element's label.  Those are typed values,
    # not identity hints — let the alias/CSS-token path run instead.
    if text_hint and text_hint.strip():
        _is_typed_value = step_type in {"type", "select"} and bool(
            re.search(r"[^@\s]+@[^@\s]+", text_hint)          # email address
            or re.match(r"https?://|www\.", text_hint, re.IGNORECASE)  # URL
            or re.search(r"\d{8}[_\-]\d{6}", text_hint)       # timestamp YYYYMMDD_HHMMSS
        )
        if not _is_typed_value:
            return text_hint.strip()

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
    # Deduplicate while preserving order — multi-attribute selectors like
    # input[name='email'][autocomplete='email'] produce repeated tokens
    # ("email email") which inflate the target_text with no extra signal.
    seen: set[str] = set()
    unique_tokens: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            unique_tokens.append(t)
    return " ".join(unique_tokens[:4]).strip() or None


def intent_element_type(step_type: str, raw_selector: str, text_hint: str | None) -> str:
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


def build_step_intent(
    step_type: str,
    raw_selector: str | None,
    text_hint: str | None = None,
    context_hint: str | None = None,
) -> StepIntent:
    selector = (raw_selector or "").strip()
    # Prefer clean visible text over raw CSS selector syntax when computing
    # ordinal/scope signals — raw selectors pollute intent extraction.
    extracted_text = extract_selector_text(selector) if selector else None
    source = " ".join(part for part in (extracted_text or selector, text_hint or "") if part).strip()
    return StepIntent(
        action=step_type,
        element_type=intent_element_type(step_type, selector, text_hint),
        target_text=intent_target_text(step_type, selector, text_hint),
        ordinal=intent_ordinal(source),
        scope_hint=intent_scope_hint(source, context_hint),
        raw_selector=selector or None,
        text_hint=text_hint,
    )


def serialize_step_intent(intent: StepIntent | None) -> dict[str, Any] | None:
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
