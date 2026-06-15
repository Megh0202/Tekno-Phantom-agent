from __future__ import annotations

import re
from typing import Any


# ---------------------------------------------------------------------------
# String normalisation helpers
# ---------------------------------------------------------------------------

def snake_to_camel(value: str) -> str:
    parts = [part for part in value.split("_") if part]
    if not parts:
        return value
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def camel_to_snake(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


def escape_playwright_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def dedupe(values: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for item in values:
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


# ---------------------------------------------------------------------------
# Alias / template detection
# ---------------------------------------------------------------------------

def selector_alias_key(selector: str) -> str | None:
    """Return the alias key if the selector is a template alias (e.g. ``{{selector.email}}``),
    a ``$name`` shorthand, or a ``profile:name`` reference.  Returns None otherwise."""
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


# ---------------------------------------------------------------------------
# Selector shape analysis
# ---------------------------------------------------------------------------

def looks_like_explicit_selector(selector: str) -> bool:
    """Return True when the selector is a concrete CSS/XPath selector rather than
    a semantic target string or alias that needs perception-based resolution."""
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


def extract_selector_text(selector: str) -> str | None:
    """Extract the visible text value from a Playwright text-based selector such as
    ``:has-text('…')``, ``:text('…')``, ``:text-is('…')``, or ``text=…``."""
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


def parse_selector_attributes(selector: str) -> dict[str, str]:
    """Extract key attribute constraints from a CSS/Playwright selector.

    Returns ``{snapshot_field: value}`` pairs that must ALL match for a snapshot
    element to be considered a resolution of this selector.

    Recognised patterns:
      #idvalue              → id
      [name='v']            → name
      [type='v']            → type
      [data-testid='v']     → testid
      [aria-label='v']      → aria
      [placeholder='v']     → placeholder
      Leading tag           → tag  (input/button/a/select/textarea only)
    """
    attrs: dict[str, str] = {}
    s = selector.strip()
    m = re.search(r"#([A-Za-z][A-Za-z0-9_-]*)", s)
    if m:
        attrs["id"] = m.group(1)
    for m in re.finditer(r'''\[([a-zA-Z_-]+)=['"]([^'"]+)['"]\]''', s):
        attr, val = m.group(1).lower(), m.group(2).strip()
        if attr == "name":
            attrs["name"] = val
        elif attr == "type":
            attrs["type"] = val
        elif attr in ("data-testid", "data-cy", "data-test"):
            attrs["testid"] = val
        elif attr == "aria-label":
            attrs["aria"] = val
        elif attr == "placeholder":
            attrs["placeholder"] = val
    tag_m = re.match(r"^([a-zA-Z]+)", s)
    if tag_m:
        tag = tag_m.group(1).lower()
        if tag in {"input", "button", "a", "select", "textarea"}:
            attrs["tag"] = tag
    return attrs


def resolve_selector_in_snapshot(
    resolved_selector: str,
    elements: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Find snapshot elements that ``resolved_selector`` would resolve to.

    Phase 1 — exact: selector string appears in the element's own selectors list.
    Phase 2 — attribute: parse the selector and match snapshot fields.

    Returns matched elements.  Empty = not found.  Multiple = ambiguous.
    """
    exact = [
        el for el in elements
        if isinstance(el, dict)
        and resolved_selector in (el.get("selectors") or [])
    ]
    if exact:
        return exact
    attrs = parse_selector_attributes(resolved_selector)
    if not attrs:
        return []
    matched = []
    for el in elements:
        if not isinstance(el, dict):
            continue
        if all(
            str(el.get(field, "")).strip().lower() == val.strip().lower()
            for field, val in attrs.items()
        ):
            matched.append(el)
    return matched


def selector_stability_score(selector: str) -> int:
    """Score a selector by how stable it is across page renders.  Higher = more stable."""
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


def is_unsafe_memory_selector(selector: str) -> bool:
    """Return True when a selector is too broad to store in selector memory
    (e.g. ``html``, ``body``)."""
    token = selector.strip().lower()
    return token in {
        "html",
        "body",
        "xpath=//html",
        "xpath=/html",
        "xpath=//body",
        "xpath=/body",
    } or token.startswith("html.") or token.startswith("body.")


# ---------------------------------------------------------------------------
# Site-specific ordinal selectors
# ---------------------------------------------------------------------------

def amazon_result_position(selector_lower: str, text_hint: str | None = None) -> int | None:
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


def amazon_result_candidates(position: int) -> list[str]:
    index = max(position - 1, 0)
    nth = max(position, 1)
    return [
        f"div[data-component-type='s-search-result'] h2 a >> nth={index}",
        f"div[data-component-type='s-search-result'] [data-cy='title-recipe-title'] a >> nth={index}",
        f"div[data-component-type='s-search-result']:nth-of-type({nth}) h2 a",
        f"div[data-component-type='s-search-result']:nth-of-type({nth}) [data-cy='title-recipe-title'] a",
    ]


def youtube_result_position(selector_lower: str, text_hint: str | None = None) -> int | None:
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


def youtube_result_candidates(position: int) -> list[str]:
    index = max(position - 1, 0)
    nth = max(position, 1)
    return [
        f"a#video-title >> nth={index}",
        f"ytd-video-renderer a#video-title >> nth={index}",
        f"ytd-video-renderer:nth-of-type({nth}) a#video-title",
    ]
