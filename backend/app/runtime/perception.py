"""
perception.py — Perceive-first element identification layer.

Instead of generating a selector from a text description and hoping it matches
the DOM, this module observes the live page (via inspect_page() snapshot) and
identifies the element that best matches the step's intent BEFORE any action is
attempted.

The selector is derived FROM the identified element, not used to find it.

Call order:
    snapshot = await browser.inspect_page(include_screenshot=False)
    index    = build_element_index(snapshot)
    match    = find_best_match(intent_text, step_type, index)

    if match and match.confidence in {"unique", "high"}:
        # execute directly with match.selector
    else:
        # fall back to existing pipeline
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

LOGGER = logging.getLogger("tekno.phantom.perception")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IndexedElement:
    """A single interactive element observed from a live inspect_page() snapshot."""
    tag: str
    role: str
    text: str
    aria: str
    name: str
    el_id: str
    testid: str
    placeholder: str
    title: str
    el_type: str        # value of input[type]
    visible: bool
    enabled: bool
    selectors: tuple[str, ...]   # ordered most-stable → least-stable

    @property
    def best_selector(self) -> str | None:
        return self.selectors[0] if self.selectors else None

    def signature(self) -> dict[str, str]:
        """
        Stable identity fingerprint stored in selector memory instead of raw
        selector strings. Survives minor DOM / class-name changes.
        """
        return {
            "tag": self.tag,
            "role": self.role,
            "text": self.text[:80],
            "aria": self.aria[:80],
            "name": self.name,
            "id": self.el_id,
            "testid": self.testid,
            "placeholder": self.placeholder[:60],
        }


@dataclass
class PerceptionMatch:
    """The result of matching a step intent to a specific live DOM element."""
    element: IndexedElement
    selector: str
    score: int
    confidence: str        # "unique" | "high" | "medium" | "ambiguous"
    alternative_count: int # how many other elements also scored above threshold


@dataclass
class ElementIndex:
    """Structured, deduplicated index of every visible interactive element on the page."""
    url: str
    elements: list[IndexedElement] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.elements)


# ---------------------------------------------------------------------------
# Selector stability ranking
# ---------------------------------------------------------------------------

def _selector_stability_rank(sel: str) -> int:
    """
    Lower number = more stable selector. Used to sort a candidate list so
    the most reliable selector is tried first.
    """
    s = sel.strip()
    if s.startswith("#"):
        return 0                                        # id selector — most stable
    if "[data-testid" in s or "[data-qa" in s:
        return 1                                        # test-specific attributes
    if "[aria-label" in s:
        return 2                                        # ARIA label
    if re.match(r"^\w+\[name=", s):
        return 3                                        # tag[name=] — form field name
    if "placeholder" in s:
        return 4
    if ":has-text(" in s and s.startswith(("button", "a", "[role")):
        return 5                                        # semantic text match on button/link
    if s.startswith("text="):
        return 6
    return 7                                            # everything else


def _selector_uses_duplicate_id(sel: str, duplicate_id_counts: dict[str, int] | None) -> bool:
    selector = sel.strip()
    if not selector.startswith("#"):
        return False
    selector_id = selector[1:]
    return int((duplicate_id_counts or {}).get(selector_id, 0)) > 1


def _build_selectors_for_element(
    item: dict[str, Any],
    duplicate_id_counts: dict[str, int] | None = None,
) -> tuple[str, ...]:
    """
    Merge DOM-provided selectors with selectors we construct from stable
    attributes, then sort by stability.
    """
    raw = [
        str(selector).strip()
        for selector in (item.get("selectors") or [])
        if str(selector).strip() and not _selector_uses_duplicate_id(str(selector), duplicate_id_counts)
    ]

    eid        = str(item.get("id", "")).strip()
    testid     = str(item.get("testid", "")).strip()
    aria       = str(item.get("aria", "")).strip()
    name       = str(item.get("name", "")).strip()
    tag        = str(item.get("tag", "")).strip().lower()
    placeholder = str(item.get("placeholder", "")).strip()
    text       = str(item.get("text", "")).strip()
    role       = str(item.get("role", "")).strip()

    extras: list[str] = []
    if eid and int((duplicate_id_counts or {}).get(eid, 0)) <= 1:
        extras.append(f"#{eid}")
    if testid:
        extras.append(f"[data-testid='{testid}']")
    if aria:
        extras.append(f"[aria-label='{aria}']")
    if tag and name:
        extras.append(f"{tag}[name='{name}']")
    if tag and placeholder:
        extras.append(f"{tag}[placeholder='{placeholder}']")
    if text and tag == "button":
        extras.append(f"button:has-text('{text}')")
    if text and tag == "a":
        extras.append(f"a:has-text('{text}')")
    if text and role in {"button", "link", "menuitem"}:
        extras.append(f"[role='{role}']:has-text('{text}')")

    merged = list(dict.fromkeys(extras + raw))  # dedupe, extras first
    merged.sort(key=_selector_stability_rank)
    return tuple(merged)


# ---------------------------------------------------------------------------
# Index construction
# ---------------------------------------------------------------------------

def build_element_index(snapshot: dict[str, Any]) -> ElementIndex:
    """
    Convert a raw inspect_page() snapshot into a typed, structured ElementIndex.
    Invisible elements are excluded — they cannot be interacted with.
    """
    url = str(snapshot.get("url", ""))
    raw_elements: list[Any] = snapshot.get("interactive_elements") or []
    duplicate_id_counts: dict[str, int] = {}
    for item in raw_elements:
        if not isinstance(item, dict):
            continue
        eid = str(item.get("id", "")).strip()
        if not eid:
            continue
        duplicate_id_counts[eid] = duplicate_id_counts.get(eid, 0) + 1

    elements: list[IndexedElement] = []
    for item in raw_elements:
        if not isinstance(item, dict):
            continue
        if not item.get("visible", True):
            continue  # invisible = not interactable right now

        selectors = _build_selectors_for_element(item, duplicate_id_counts)
        elements.append(IndexedElement(
            tag=str(item.get("tag", "")).strip().lower(),
            role=str(item.get("role", "")).strip(),
            text=str(item.get("text", "")).strip()[:120],
            aria=str(item.get("aria", "")).strip(),
            name=str(item.get("name", "")).strip(),
            el_id=str(item.get("id", "")).strip(),
            testid=str(item.get("testid", "")).strip(),
            placeholder=str(item.get("placeholder", "")).strip(),
            title=str(item.get("title", "")).strip(),
            el_type=str(item.get("type", "")).strip(),
            visible=True,
            enabled=bool(item.get("enabled", True)),
            selectors=selectors,
        ))

    LOGGER.debug("Built element index: %d visible interactive elements at %s", len(elements), url)
    return ElementIndex(url=url, elements=elements)


# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------

_STOP_WORDS = frozenset({
    # English function words
    "the", "and", "or", "in", "on", "at", "to", "a", "an", "is", "it",
    "for", "of", "with", "by", "be", "was", "are", "has", "have", "do",
    # Selector / step-description noise words
    "selector", "input", "button", "click", "type", "wait", "verify",
    "text", "select", "field", "form", "into", "element", "page",
})


def _tokenize(text: str) -> list[str]:
    """
    Extract meaningful tokens from a step intent or selector string.
    Strips CSS/template syntax and stop-words.
    """
    text = text.lower()
    # Remove CSS / template syntax characters
    text = re.sub(r"[{}\[\]()'\">#.=:@$]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return [
        t for t in text.split()
        if len(t) >= 2 and t not in _STOP_WORDS
    ]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _element_haystack(el: IndexedElement) -> str:
    """All searchable text for an element, lower-cased."""
    return " ".join([
        el.text, el.aria, el.name, el.el_id, el.testid,
        el.placeholder, el.title, el.role, el.tag, el.el_type,
    ]).lower()


def score_element(
    el: IndexedElement,
    tokens: list[str],
    step_type: str,
) -> int:
    """
    Score how well an element matches a set of intent tokens for a given
    step type.  Returns 0 if the element is disabled or no tokens matched.
    """
    if not el.enabled:
        return 0

    haystack = _element_haystack(el)
    el_text_lower = el.text.lower()
    score = 0
    matched = 0

    for token in tokens:
        if token not in haystack:
            continue
        matched += 1
        base = max(10, len(token) * 3)
        score += base
        # Visible text match carries extra weight — it's what the user sees
        if token in el_text_lower:
            score += 8
        # Exact text match — very specific
        if el_text_lower == token:
            score += 25
        # ARIA label match — high intent signal
        if token in el.aria.lower():
            score += 12
        # data-testid match — highest programmatic signal
        if token in el.testid.lower():
            score += 15
        # placeholder — common for inputs
        if token in el.placeholder.lower():
            score += 10
        # name attribute
        if token in el.name.lower():
            score += 8

    # If we have tokens but none matched → element is irrelevant
    if tokens and matched == 0:
        return 0

    # Step type alignment bonuses / penalties
    if step_type == "click":
        if el.tag in {"button", "a"}:
            score += 20
        if el.role in {"button", "link", "menuitem", "tab", "checkbox", "radio", "option"}:
            score += 15
    elif step_type == "type":
        if el.tag in {"input", "textarea"}:
            score += 25
        if el.el_type in {"text", "email", "password", "search", "tel", "url", "number", ""}:
            score += 15
        if el.role in {"textbox", "searchbox", "combobox"}:
            score += 15
        if el.tag == "button":
            score -= 20   # typing into a button is almost never correct
    elif step_type == "select":
        if el.tag == "select":
            score += 35
        if el.role == "combobox":
            score += 25
        if el.tag == "button":
            score -= 10

    # Stable-attribute bonuses (elements with stable IDs/testids are
    # preferred when two elements score similarly)
    if el.el_id:
        score += 8
    if el.testid:
        score += 10
    if el.aria:
        score += 6

    return max(score, 0)


# ---------------------------------------------------------------------------
# Confidence thresholds
# ---------------------------------------------------------------------------

_MIN_SCORE = 22        # minimum score to be a candidate at all
_HIGH_GAP = 20         # score gap between top-1 and top-2 for "high" confidence
_UNIQUE_GAP = 30       # score gap for "unique" (only 1 element is above threshold)


# ---------------------------------------------------------------------------
# Main matching entry point
# ---------------------------------------------------------------------------

def find_by_signatures(
    signatures: list[dict],
    element_index: ElementIndex,
) -> list[str]:
    """
    Given a list of previously-stored element signatures (tag, role, text, aria,
    name, id, testid, placeholder) and the current live DOM index, return the
    best-selector for each element that closely matches one of the signatures.

    Used for DOM Signature Memory: when a stored CSS selector stops working after
    a UI change, this finds the element again by its semantic identity instead.

    Returns a deduplicated, ordered list of selectors (best match first).
    """
    if not signatures or not element_index.elements:
        return []

    seen: set[str] = set()
    results: list[tuple[int, str]] = []  # (score, selector)

    for sig in signatures:
        sig_tag = str(sig.get("tag", "")).lower().strip()
        sig_role = str(sig.get("role", "")).lower().strip()
        sig_text = str(sig.get("text", "")).lower().strip()
        sig_aria = str(sig.get("aria", "")).lower().strip()
        sig_name = str(sig.get("name", "")).strip()
        sig_id = str(sig.get("id", "")).strip()
        sig_testid = str(sig.get("testid", "")).strip()
        sig_placeholder = str(sig.get("placeholder", "")).lower().strip()

        best_score = 0
        best_selector: str | None = None

        for el in element_index.elements:
            score = 0

            # Hard stable-attribute matches (high value: element identity is clear)
            if sig_id and el.el_id and sig_id == el.el_id:
                score += 60
            if sig_testid and el.testid and sig_testid == el.testid:
                score += 55
            if sig_aria and el.aria.lower() and sig_aria == el.aria.lower():
                score += 40
            elif sig_aria and el.aria.lower() and sig_aria in el.aria.lower():
                score += 20

            # Structural / semantic matches
            if sig_tag and el.tag and sig_tag == el.tag:
                score += 10
            if sig_role and el.role and sig_role == el.role:
                score += 10
            if sig_name and el.name and sig_name == el.name:
                score += 20

            # Visible text match (partial OK)
            if sig_text and el.text.lower():
                el_text = el.text.lower()
                if sig_text == el_text:
                    score += 35
                elif sig_text in el_text or el_text in sig_text:
                    score += 15

            # Placeholder match
            if sig_placeholder and el.placeholder.lower():
                if sig_placeholder == el.placeholder.lower():
                    score += 25
                elif sig_placeholder in el.placeholder.lower():
                    score += 10

            # Require at least TWO distinct non-trivial signals to avoid false matches
            strong_signals = sum([
                bool(sig_id and el.el_id and sig_id == el.el_id),
                bool(sig_testid and el.testid and sig_testid == el.testid),
                bool(sig_aria and el.aria.lower() and sig_aria == el.aria.lower()),
                bool(sig_text and sig_text in el.text.lower()),
                bool(sig_name and el.name and sig_name == el.name),
                bool(sig_placeholder and sig_placeholder in el.placeholder.lower()),
            ])
            if strong_signals < 2 and score < 50:
                continue

            if score > best_score:
                best_score = score
                best_selector = el.best_selector

        if best_selector and best_score >= 30 and best_selector not in seen:
            seen.add(best_selector)
            results.append((best_score, best_selector))

    results.sort(key=lambda x: x[0], reverse=True)
    selectors = [sel for _, sel in results]

    if selectors:
        LOGGER.info(
            "Signature recovery: matched %d selector(s) from %d stored signature(s)",
            len(selectors), len(signatures),
        )
    else:
        LOGGER.debug(
            "Signature recovery: no matches found from %d stored signature(s) "
            "against %d DOM elements",
            len(signatures), element_index.count,
        )
    return selectors


def find_best_match(
    intent_text: str,
    step_type: str,
    element_index: ElementIndex,
    min_score: int = _MIN_SCORE,
) -> PerceptionMatch | None:
    """
    Find the live DOM element that best matches the step's intent.

    Returns a PerceptionMatch with one of four confidence levels:
    - "unique"   : only ONE element scored above the threshold (clear winner)
    - "high"     : top element scores ≥_UNIQUE_GAP above second-best
    - "medium"   : top element scores ≥_HIGH_GAP above second-best
    - "ambiguous": multiple elements score similarly (intent needs disambiguation)

    Returns None if no element meets the minimum score — the caller should
    fall back to the existing selector-candidate pipeline.
    """
    if not element_index.elements:
        LOGGER.debug("Perception: element index is empty for intent=%r", intent_text[:60])
        return None

    tokens = _tokenize(intent_text)
    if not tokens:
        LOGGER.debug("Perception: no meaningful tokens extracted from intent=%r", intent_text[:60])
        return None

    scored: list[tuple[int, IndexedElement]] = []
    for el in element_index.elements:
        s = score_element(el, tokens, step_type)
        if s >= min_score:
            scored.append((s, el))

    if not scored:
        LOGGER.debug(
            "Perception: 0 elements met min_score=%d for intent=%r step_type=%s "
            "(index has %d elements)",
            min_score, intent_text[:60], step_type, element_index.count,
        )
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    top_score, top_el = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0
    gap = top_score - second_score
    above_threshold = len(scored)

    selector = top_el.best_selector
    if not selector:
        LOGGER.debug("Perception: top element has no usable selector for intent=%r", intent_text[:60])
        return None

    # Determine confidence
    if above_threshold == 1:
        confidence = "unique"
    elif gap >= _UNIQUE_GAP:
        confidence = "high"
    elif gap >= _HIGH_GAP:
        confidence = "medium"
    else:
        confidence = "ambiguous"

    LOGGER.info(
        "Perception: %s match  step_type=%-6s  score=%d  gap=%d  alternatives=%d  "
        "selector=%r  element_text=%r  intent=%r",
        confidence.upper(), step_type,
        top_score, gap, above_threshold - 1,
        selector, top_el.text[:60], intent_text[:60],
    )

    return PerceptionMatch(
        element=top_el,
        selector=selector,
        score=top_score,
        confidence=confidence,
        alternative_count=above_threshold - 1,
    )
