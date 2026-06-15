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
    label: str          # associated <label> text for the element
    visible: bool
    enabled: bool
    selectors: tuple[str, ...]   # ordered most-stable → least-stable
    semantic_role: str = ""      # normalised ARIA-style role (textbox, button, combobox, link, …)
    scope: str = ""              # page region this element lives in (dialog/form/main/nav/aside/…)
                                 # populated from detectScope() in the browser snapshot

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
    scored_elements: list[dict] = field(default_factory=list)  # top candidates with scores for trace
    decision_trace: dict = field(default_factory=dict)  # full decision chain for step trace


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
# Semantic role normalisation
# ---------------------------------------------------------------------------

# Input[type] values that map to the "textbox" semantic role.
_TEXTBOX_INPUT_TYPES: frozenset[str] = frozenset({
    "text", "email", "password", "search", "tel", "url",
    "number", "date", "time", "week", "month", "color", "range", "",
})

# ARIA role → canonical semantic role used in the planner contract.
# Roles not in this map are treated as unknown ("").
_ARIA_TO_SEMANTIC: dict[str, str] = {
    "textbox": "textbox",
    "searchbox": "textbox",
    "combobox": "combobox",
    "listbox": "combobox",
    "button": "button",
    "link": "link",
    "checkbox": "checkbox",
    "radio": "radio",
    "menuitem": "menuitem",
    "menuitemcheckbox": "menuitem",
    "menuitemradio": "menuitem",
    "tab": "tab",
    "switch": "checkbox",
    "option": "option",
}


def _normalize_semantic_role(tag: str, el_type: str, aria_role: str) -> str:
    """
    Derive a canonical semantic role (matching the planner contract vocabulary)
    from a live DOM element's tag, input type, and ARIA role.

    Priority: explicit ARIA role > tag+type derivation.
    Returns "" when the role cannot be determined.
    """
    # Explicit ARIA role takes priority — the page already declares the semantics.
    normalized_aria = _ARIA_TO_SEMANTIC.get(aria_role.lower().strip(), "")
    if normalized_aria:
        return normalized_aria

    tag_lower = tag.lower().strip()
    type_lower = el_type.lower().strip()

    if tag_lower == "input":
        if type_lower in _TEXTBOX_INPUT_TYPES:
            return "textbox"
        if type_lower == "checkbox":
            return "checkbox"
        if type_lower == "radio":
            return "radio"
        if type_lower in {"submit", "button", "reset", "image"}:
            return "button"
        return "textbox"   # safe default for unrecognised input types
    if tag_lower == "textarea":
        return "textbox"
    if tag_lower == "select":
        return "combobox"
    if tag_lower == "button":
        return "button"
    if tag_lower == "a":
        return "link"

    return ""


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
        _tag     = str(item.get("tag",  "")).strip().lower()
        _el_type = str(item.get("type", "")).strip()
        _role    = str(item.get("role", "")).strip()
        elements.append(IndexedElement(
            tag=_tag,
            role=_role,
            text=str(item.get("text", "")).strip()[:120],
            aria=str(item.get("aria", "")).strip(),
            name=str(item.get("name", "")).strip(),
            el_id=str(item.get("id", "")).strip(),
            testid=str(item.get("testid", "")).strip(),
            placeholder=str(item.get("placeholder", "")).strip(),
            title=str(item.get("title", "")).strip(),
            el_type=_el_type,
            label=str(item.get("label", "")).strip()[:80],
            visible=True,
            enabled=bool(item.get("enabled", True)),
            selectors=selectors,
            semantic_role=_normalize_semantic_role(_tag, _el_type, _role),
            scope=str(item.get("scope", "")).strip().lower(),
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
    # HTML attribute names — appear in CSS selectors but carry no element-
    # identity signal; only the attribute VALUE matters for matching
    "name", "id", "class", "href", "value", "action", "method", "src", "alt",
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
# Region-priority scoring
# ---------------------------------------------------------------------------
#
# Additive score deltas applied based on the page region (scope) where an
# element lives.  The values are intentionally moderate — they tip the
# balance between otherwise similar candidates but cannot override a strong
# semantic text/label match.
#
# Positive = interaction regions (prefer for action steps)
# Negative = navigation/layout chrome (de-prioritize for action steps)
#
# Key principle:
#   Navigation intent ("open Checkbox page") → nav link still wins because
#   text+tag+role bonuses (~70) dwarf the −20 penalty.
#   Interaction intent ("toggle Primary checkbox") → control in main/form wins
#   because interaction-owner boosts + label scoring >> nav link score.
#
# NEVER used as a hard filter — only shifts the relative scoring balance.
_REGION_SCORE_DELTA: dict[str, int] = {
    # Active interaction containers — boost
    "dialog": 20,       # modal/dialog — highest priority (also has dialog-scope boost in selectors)
    "form": 12,         # active form region
    "search": 8,        # search region
    "main": 8,          # primary content area
    "article": 6,       # content article
    "listbox": 6,       # open dropdown — already scoped by _dialog_scoped candidate restrict
    # Layout chrome — de-prioritize
    "nav": -20,         # navigation sidebar / top nav
    "aside": -20,       # sidebar / secondary panel
    "header": -8,       # page header chrome
    "footer": -8,       # page footer chrome
    # Neutral / unknown
    "body": 0,
    "": 0,
}


# ---------------------------------------------------------------------------
# Interaction-category ownership constants
# (defined here so score_element and score_element_for_target can reference them)
# ---------------------------------------------------------------------------

# ARIA roles that unambiguously identify the SEMANTIC INTERACTION OWNER for
# toggle/selection actions.  These elements are the correct click target for
# any checkbox-like intent — they receive a score boost that outweighs
# text-only matches from surrounding containers/wrappers.
#
# Sized to exceed the gap between a container div with a perfect text match
# (~78) and a checkbox button with a partial text/aria match (~62), pushing
# the toggle-owner to clear first place even when its visible label text is
# sparse relative to a surrounding wrapper.
_TOGGLE_INTERACTION_OWNER_ROLES: frozenset[str] = frozenset({
    "checkbox",
    "switch",
    "radio",
    "menuitemcheckbox",
    "menuitemradio",
    "treeitem",       # treeitem can carry aria-checked in tree/outline controls
})
_TOGGLE_OWNER_BOOST = 30   # added on top of generic click role bonus


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _element_haystack(el: IndexedElement) -> str:
    """All searchable text for an element, lower-cased."""
    return " ".join([
        el.text, el.aria, el.name, el.el_id, el.testid,
        el.placeholder, el.title, el.role, el.tag, el.el_type, el.label,
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
        # associated label text — strong identity signal for form inputs
        if el.label and token in el.label.lower():
            score += 10

    # If we have tokens but none matched → element is irrelevant
    if tokens and matched == 0:
        return 0

    # Phrase match bonus: all tokens appearing together in order in element text
    # is a much stronger signal than individual token matches.
    if len(tokens) >= 2:
        phrase = " ".join(tokens)
        if phrase in el_text_lower:
            score += 22   # exact phrase in visible text — very high signal
        elif phrase in haystack:
            score += 10   # phrase found elsewhere in element attributes

    # Exact full-text match: element text IS the intent (e.g. button says "Sign In"
    # and intent is "sign in") — strongest possible signal.
    if tokens and el_text_lower == " ".join(tokens):
        score += 30

    # Label phrase-match + cardinality scoring.
    # The associated <label> text (from aria-labelledby, <label for>, or a
    # nearest-container heuristic) is the strongest identity signal for any
    # labelled control — not just inputs.
    #
    # Applied to type/select (form fields) AND click (buttons, checkboxes,
    # toggles, radios) so that a button[role=checkbox] whose aria-labelledby
    # resolves to "Required" beats a nearby tab/container with "Required"
    # in its visible text.
    #
    # Cardinality principle (generic, no hardcoded field names):
    #   - Label whose token set EXACTLY matches the intent tokens → strongest
    #     signal; the field is named precisely what the intent describes.
    #   - Label that is a SUPERSET of the intent (extra words) → weaker;
    #     this is a more-specific sibling field (e.g. intent="password" but
    #     label="Confirm Password" — label has an extra distinguishing word).
    #   - Label that is a SUBSET of the intent (missing words) → penalised;
    #     the field label does not fully cover what was asked for.
    if el.label and step_type in {"type", "select", "click"}:
        label_lower = el.label.lower()
        label_tokens = {t for t in label_lower.split() if len(t) >= 2}
        intent_tokens = set(tokens)
        phrase = " ".join(tokens)

        if phrase and phrase in label_lower:
            # Full intent phrase found verbatim in label — strongest match
            score += 20
        elif any(t in label_lower for t in tokens):
            score += 8    # at least one token present in label

        # Cardinality adjustment (only when label tokens are meaningful)
        if label_tokens and intent_tokens:
            matched = intent_tokens & label_tokens
            extra_in_label = label_tokens - intent_tokens   # label more specific
            missing_from_label = intent_tokens - label_tokens  # label too narrow

            if matched == intent_tokens and not extra_in_label:
                # Perfect token match: label names exactly what intent asks for
                score += 12
            elif extra_in_label and matched == intent_tokens:
                # Label is a superset — this is a sibling field with extra
                # qualifier words. Penalise proportionally to the extra words.
                score -= min(len(extra_in_label) * 6, 18)
            elif missing_from_label:
                # Label is missing some intent words — partial overlap
                score -= min(len(missing_from_label) * 4, 12)

    # Step type alignment bonuses / penalties
    if step_type == "click":
        if el.tag in {"button", "a"}:
            score += 20
        if el.role in {"button", "link", "menuitem", "tab", "checkbox", "radio", "option"}:
            score += 15
        # Interaction-owner boost: toggle controls (checkbox/switch/radio) are
        # the semantic owner of their toggle action.  They receive an additional
        # boost so that a wrapper/container element sharing the same visible text
        # does not outrank the actual actionable node.
        if el.role in _TOGGLE_INTERACTION_OWNER_ROLES:
            score += _TOGGLE_OWNER_BOOST
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

    # Region-priority delta: boost elements in active interaction regions,
    # de-prioritize navigation/layout chrome.  Applied last so it shifts
    # relative ordering without overriding strong semantic text matches.
    score += _REGION_SCORE_DELTA.get(el.scope, 0)

    return max(score, 0)


# ---------------------------------------------------------------------------
# Confidence thresholds
# ---------------------------------------------------------------------------

_MIN_SCORE = 18        # minimum score to be a candidate at all
_HIGH_SCORE = 38       # minimum absolute score to award "high" or "unique" confidence
                       # — prevents a single weak token match from producing
                       # actionable confidence when competitors score 0.
_HIGH_GAP = 20         # score gap between top-1 and top-2 for "high" confidence
                       # — raised from 15 to require stronger separation between
                       # semantically adjacent elements (e.g. "Save" vs "Save Changes")
_UNIQUE_GAP = 30       # score gap for "unique" confidence (was 22)
                       # — single-element candidacy alone is insufficient if the
                       # absolute score is low (partial token match, noisy page)

# Score boost applied to elements whose selectors reference the active modal
# container when an active dialog/alertdialog is present on the page.  The
# boost is sized to exceed _HIGH_GAP (20) when a dialog-scoped element
# competes against a background-page element with a similar raw score —
# pushing the gap past the threshold and promoting the correct element to
# "high" or "unique" confidence.
_DIALOG_SCOPE_BOOST = 25

# Compiled pattern to detect dialog-scope markers in Playwright-generated
# selectors.  Playwright routinely includes [role="dialog"] as a selector
# prefix for elements that are structurally inside a dialog container.
_DIALOG_SCOPE_RE = re.compile(
    r'\[role=["\']?(?:dialog|alertdialog)["\']?\]|\[aria-modal',
    re.IGNORECASE,
)


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


def _score_breakdown(el: IndexedElement, tokens: list[str], step_type: str) -> dict:
    """Return a field-level contribution map for the top candidates only (not called on all 83)."""
    haystack = _element_haystack(el)
    el_text_lower = el.text.lower()
    matched_tokens = [t for t in tokens if t in haystack]
    contributions: dict[str, int] = {}

    for token in matched_tokens:
        base = max(10, len(token) * 3)
        contributions["token_base"] = contributions.get("token_base", 0) + base
        if token in el_text_lower:
            contributions["visible_text"] = contributions.get("visible_text", 0) + 8
        if el_text_lower == token:
            contributions["exact_text"] = contributions.get("exact_text", 0) + 25
        if token in el.aria.lower():
            contributions["aria_label"] = contributions.get("aria_label", 0) + 12
        if token in el.testid.lower():
            contributions["testid"] = contributions.get("testid", 0) + 15
        if token in el.placeholder.lower():
            contributions["placeholder"] = contributions.get("placeholder", 0) + 10
        if token in el.name.lower():
            contributions["name_attr"] = contributions.get("name_attr", 0) + 8
        if el.label and token in el.label.lower():
            contributions["label"] = contributions.get("label", 0) + 10

    if len(tokens) >= 2:
        phrase = " ".join(tokens)
        if phrase in el_text_lower:
            contributions["phrase_text"] = 22
        elif phrase in haystack:
            contributions["phrase_attr"] = 10

    if step_type == "click" and el.tag in {"button", "a"}:
        contributions["tag_alignment"] = contributions.get("tag_alignment", 0) + 20
    elif step_type == "type" and el.tag in {"input", "textarea"}:
        contributions["tag_alignment"] = contributions.get("tag_alignment", 0) + 25

    if el.el_id:
        contributions["stable_id"] = 8
    if el.testid:
        contributions["stable_testid"] = 10
    region_delta = _REGION_SCORE_DELTA.get(el.scope, 0)
    if region_delta:
        contributions["region_scope"] = region_delta

    return {"matched_tokens": matched_tokens, "field_contributions": contributions}


def find_best_match(
    intent_text: str,
    step_type: str,
    element_index: ElementIndex,
    min_score: int = _MIN_SCORE,
    active_dialog: bool = False,
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

    When active_dialog=True, elements whose Playwright-generated selectors
    contain dialog-scope markers (e.g. [role="dialog"]) receive a score
    boost — prioritising modal-scoped elements over background-page elements
    that share similar visible text.
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
        if active_dialog and any(_DIALOG_SCOPE_RE.search(sel) for sel in el.selectors):
            s += _DIALOG_SCOPE_BOOST
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

    # Determine confidence.
    # Absolute score guard: regardless of gap or uniqueness, do not award
    # "high" or "unique" when the top score is below _HIGH_SCORE.  A low
    # absolute score means the match is based on weak/partial signals —
    # not strong enough to suppress the fallback pipeline.
    if above_threshold == 1 and top_score >= _HIGH_SCORE:
        confidence = "unique"
    elif gap >= _UNIQUE_GAP and top_score >= _HIGH_SCORE:
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

    scored_trace = [
        {
            "score": s,
            "tag": el.tag,
            "role": el.role or el.semantic_role,
            "text": el.text[:80],
            "aria": el.aria[:80],
            "label": el.label[:80],
            "id": el.el_id,
            "name": el.name,
            "scope": el.scope,
            "selector": el.best_selector or "",
        }
        for s, el in scored[:12]
    ]

    top5_trace = []
    for s, el in scored[:5]:
        breakdown = _score_breakdown(el, tokens, step_type)
        top5_trace.append({
            "score": s,
            "selector": el.best_selector or "",
            "tag": el.tag,
            "role": el.role or el.semantic_role or "",
            "text": el.text[:80],
            "label": el.label[:80],
            "id": el.el_id,
            "scope": el.scope,
            "matched_tokens": breakdown["matched_tokens"],
            "field_contributions": breakdown["field_contributions"],
        })

    if confidence == "unique":
        confidence_reason = f"only 1 element above threshold (score={top_score})"
    elif confidence == "high":
        confidence_reason = f"gap={gap} >= unique_gap={_UNIQUE_GAP}, score={top_score} >= high_score={_HIGH_SCORE}"
    elif confidence == "medium":
        confidence_reason = f"gap={gap} >= high_gap={_HIGH_GAP} but < unique_gap={_UNIQUE_GAP}"
    else:
        confidence_reason = f"gap={gap} < high_gap={_HIGH_GAP} — {above_threshold} candidates score similarly"

    decision_trace = {
        "tokens": tokens,
        "candidates_evaluated": element_index.count,
        "candidates_above_threshold": above_threshold,
        "top_candidates": top5_trace,
        "winner_selector": selector,
        "winner_score": top_score,
        "second_score": second_score,
        "gap": gap,
        "confidence": confidence,
        "confidence_reason": confidence_reason,
        "thresholds": {
            "min_score": min_score,
            "high_score": _HIGH_SCORE,
            "high_gap": _HIGH_GAP,
            "unique_gap": _UNIQUE_GAP,
        },
    }

    return PerceptionMatch(
        element=top_el,
        selector=selector,
        score=top_score,
        confidence=confidence,
        alternative_count=above_threshold - 1,
        scored_elements=scored_trace,
        decision_trace=decision_trace,
    )


# ---------------------------------------------------------------------------
# Structured semantic target matching
# ---------------------------------------------------------------------------

def _extract_target_canonical(target: dict[str, Any]) -> dict[str, str | None]:
    """
    Resolve a target dict — from either the new semantic contract or legacy plans
    — into the canonical field set used by the structured scorer.

    New fields (semantic_name / expected_role / accessible_name / scope) take
    priority; legacy fields (kind / role / label / text / context) are used as
    fallbacks so that old plans continue to work.
    """
    def _s(v: Any) -> str | None:
        if not isinstance(v, str):
            return None
        s = v.strip()
        return s or None

    return {
        "semantic_name":  _s(target.get("semantic_name"))  or _s(target.get("kind")),
        "expected_role":  _s(target.get("expected_role"))  or _s(target.get("role")),
        "accessible_name": (
            _s(target.get("accessible_name"))
            or _s(target.get("label"))
            or _s(target.get("text"))
        ),
        "placeholder": _s(target.get("placeholder")),
        "scope":        _s(target.get("scope"))    or _s(target.get("context")),
    }


def score_element_for_target(
    el: IndexedElement,
    canonical: dict[str, str | None],
    step_type: str,
) -> int:
    """
    Score an element against a structured semantic target canonical dict.

    Fields are compared directly (no tokenisation of CSS syntax).

    accessible_name is the primary identity signal and contributes the most.
    semantic_name is secondary (disambiguates sibling fields).
    placeholder and scope are tertiary tiebreakers.
    """
    if not el.enabled:
        return 0

    accessible_name    = (canonical.get("accessible_name") or "").strip().lower()
    semantic_name      = (canonical.get("semantic_name")   or "").strip().lower()
    placeholder_target = (canonical.get("placeholder")     or "").strip().lower()
    scope              = (canonical.get("scope")           or "").strip().lower()

    el_text        = el.text.lower()
    el_aria        = el.aria.lower()
    el_label       = el.label.lower()
    el_name        = el.name.lower()
    el_placeholder = el.placeholder.lower()

    # Combined text-identity haystack for partial/token matching
    text_haystack = " ".join([el_text, el_aria, el_label, el_name]).strip()

    score = 0

    # ------------------------------------------------------------------
    # accessible_name — primary identity signal
    # Compared against every text-carrying attribute, in priority order.
    # ------------------------------------------------------------------
    if accessible_name:
        if el_text == accessible_name:
            score += 60       # exact visible text — strongest
        elif el_aria == accessible_name:
            score += 58       # exact ARIA label
        elif el_label == accessible_name:
            score += 58       # exact associated label
        elif el_name == accessible_name:
            score += 40       # exact name attribute (common for form fields)
        elif accessible_name in text_haystack:
            score += 25       # full phrase present somewhere in the element
        else:
            # Token-level overlap: score proportionally to fraction matched
            acc_tokens = [w for w in accessible_name.split() if len(w) >= 3]
            if acc_tokens:
                matched = [w for w in acc_tokens if w in text_haystack]
                if matched:
                    score += max(int(12 * len(matched) / len(acc_tokens)), 8)

    # ------------------------------------------------------------------
    # semantic_name — secondary signal; distinguishes sibling fields
    # (e.g. "password" vs "confirm password")
    # ------------------------------------------------------------------
    if semantic_name:
        full_haystack = " ".join([el_text, el_aria, el_label, el_name, el_placeholder])
        if el_label == semantic_name or el_text == semantic_name:
            score += 35       # canonical name exactly matches visible label/text
        elif semantic_name in full_haystack:
            score += 18
        else:
            sem_tokens = [w for w in semantic_name.split() if len(w) >= 3]
            if sem_tokens:
                matched = [w for w in sem_tokens if w in full_haystack]
                if matched:
                    score += int(8 * len(matched) / len(sem_tokens))

    # ------------------------------------------------------------------
    # placeholder — useful when there is no label (bare input with placeholder)
    # ------------------------------------------------------------------
    if placeholder_target:
        if el_placeholder == placeholder_target:
            score += 30
        elif placeholder_target in el_placeholder:
            score += 15

    # ------------------------------------------------------------------
    # scope — weak tiebreaker; checks if scope words appear in the
    # element's id / name / aria (structural containers often surface in those)
    # ------------------------------------------------------------------
    if scope and score > 0:
        scope_words = [w for w in scope.split() if len(w) >= 3]
        el_context = " ".join([el.el_id, el.name, el_aria]).lower()
        if any(w in el_context for w in scope_words):
            score += 6

    # ------------------------------------------------------------------
    # Step-type alignment — mirrors score_element interaction-owner logic.
    # Added here so that structured-contract paths (find_best_match_for_target)
    # also benefit from interaction-category signals.
    # ------------------------------------------------------------------
    if step_type == "click":
        if el.tag in {"button", "a"}:
            score += 20
        if el.role in {"button", "link", "menuitem", "tab", "checkbox", "radio", "option"}:
            score += 15
        if el.role in _TOGGLE_INTERACTION_OWNER_ROLES:
            score += _TOGGLE_OWNER_BOOST
    elif step_type == "type":
        if el.tag in {"input", "textarea"}:
            score += 25
        if el.el_type in {"text", "email", "password", "search", "tel", "url", "number", ""}:
            score += 15
        if el.role in {"textbox", "searchbox", "combobox"}:
            score += 15
        if el.tag == "button":
            score -= 20
    elif step_type == "select":
        if el.tag == "select":
            score += 35
        if el.role == "combobox":
            score += 25
        if el.tag == "button":
            score -= 10

    # Stable-attribute bonuses — prefer elements the runtime can address reliably
    if el.el_id:
        score += 5
    if el.testid:
        score += 8
    if el.aria:
        score += 4

    # Region-priority delta — same mechanism as score_element.
    score += _REGION_SCORE_DELTA.get(el.scope, 0)

    return max(score, 0)


def derive_element_selectors(
    element: IndexedElement,
    step_type: str,
) -> list[str]:
    """
    Derive an ordered list of real executable selectors from an already-identified
    IndexedElement.

    These selectors are grounded in the matched DOM element — not generated from
    heuristics or profile patterns.  They are used as the complete candidate list
    for constrained retry when perception produced a strong match (unique / high).

    Priority order mirrors _selector_stability_rank:
      1. #id
      2. [data-testid]
      3. [aria-label]
      4. tag[name]
      5. tag[placeholder]
      6. role/text combos (button:has-text, a:has-text, [role]:has-text)
      7. text= (Playwright text locator)
      8. Remaining selectors from the element's pre-built tuple

    Deduplicates and excludes empty strings.
    """
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(sel: str) -> None:
        s = sel.strip()
        if s and s not in seen:
            seen.add(s)
            candidates.append(s)

    # --- Stable identity selectors built from element attributes ---------------
    if element.el_id:
        _add(f"#{element.el_id}")
    if element.testid:
        _add(f"[data-testid='{element.testid}']")
    if element.aria:
        _add(f"[aria-label='{element.aria}']")
    if element.tag and element.name:
        _add(f"{element.tag}[name='{element.name}']")
    if element.tag and element.placeholder:
        _add(f"{element.tag}[placeholder='{element.placeholder}']")

    # --- Role + text selectors — semantic, survives CSS-class reshuffles -------
    text = element.text.strip()
    tag = element.tag.lower()
    role = element.role.lower() if element.role else ""

    if text:
        if tag == "button":
            _add(f"button:has-text('{text}')")
        if tag == "a":
            _add(f"a:has-text('{text}')")
        if role in {"button", "link", "menuitem", "tab"}:
            _add(f"[role='{role}']:has-text('{text}')")
        if step_type in {"click", "type", "select"} and len(text) <= 60:
            _add(f"text={text}")

    # --- Pre-built selectors from build_element_index (sorted by stability) ---
    for sel in element.selectors:
        _add(sel)

    return candidates


def find_best_match_for_target(
    target: dict[str, Any],
    step_type: str,
    element_index: ElementIndex,
    min_score: int = _MIN_SCORE,
    active_dialog: bool = False,
) -> PerceptionMatch | None:
    """
    Find the live DOM element that best matches a structured semantic target contract.

    Unlike find_best_match() (which tokenises a flat intent string), this function
    matches directly against the contract fields:

        accessible_name  →  primary identity
        semantic_name    →  disambiguation (sibling fields)
        expected_role    →  hard pre-filter (role mismatch → excluded)
        placeholder      →  tiebreaker for unlabelled inputs
        scope            →  weak container context tiebreaker

    Returns a PerceptionMatch with the same confidence levels as find_best_match,
    or None when no element meets the minimum score.

    When active_dialog=True, elements with dialog-scoped selectors receive a
    score boost — same mechanism as find_best_match.
    """
    if not element_index.elements:
        LOGGER.debug("Perception (target): element index is empty")
        return None

    canonical = _extract_target_canonical(target)
    expected_role = (canonical.get("expected_role") or "").strip().lower()

    # ------------------------------------------------------------------
    # Hard pre-filter: semantic role must match.
    # Falls through to full index if role filter empties the set — this
    # protects against role normalisation gaps during the migration period.
    # ------------------------------------------------------------------
    if expected_role:
        role_filtered = [
            el for el in element_index.elements
            if el.semantic_role == expected_role
        ]
        if role_filtered:
            candidates = role_filtered
        else:
            LOGGER.warning(
                "Perception (target): expected_role=%r matched 0 of %d elements "
                "— falling through to full index (role normalisation gap?)",
                expected_role, element_index.count,
            )
            candidates = element_index.elements
    else:
        candidates = element_index.elements

    scored: list[tuple[int, IndexedElement]] = []
    for el in candidates:
        s = score_element_for_target(el, canonical, step_type)
        if active_dialog and any(_DIALOG_SCOPE_RE.search(sel) for sel in el.selectors):
            s += _DIALOG_SCOPE_BOOST
        if s >= min_score:
            scored.append((s, el))

    if not scored:
        LOGGER.debug(
            "Perception (target): 0 elements met min_score=%d  "
            "step_type=%s  semantic_name=%r  accessible_name=%r  expected_role=%r  "
            "(role_candidates=%d  total_elements=%d)",
            min_score, step_type,
            canonical.get("semantic_name"), canonical.get("accessible_name"), expected_role,
            len(candidates), element_index.count,
        )
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    top_score, top_el = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0
    gap = top_score - second_score
    above_threshold = len(scored)

    selector = top_el.best_selector
    if not selector:
        LOGGER.debug(
            "Perception (target): top element has no usable selector  "
            "semantic_name=%r  accessible_name=%r",
            canonical.get("semantic_name"), canonical.get("accessible_name"),
        )
        return None

    if above_threshold == 1 and top_score >= _HIGH_SCORE:
        confidence = "unique"
    elif gap >= _UNIQUE_GAP and top_score >= _HIGH_SCORE:
        confidence = "high"
    elif gap >= _HIGH_GAP:
        confidence = "medium"
    else:
        confidence = "ambiguous"

    LOGGER.info(
        "Perception (target): %s match  step_type=%-6s  score=%d  gap=%d  alternatives=%d  "
        "selector=%r  element_text=%r  semantic_name=%r  accessible_name=%r  role=%r",
        confidence.upper(), step_type,
        top_score, gap, above_threshold - 1,
        selector, top_el.text[:60],
        canonical.get("semantic_name"), canonical.get("accessible_name"), expected_role or top_el.semantic_role,
    )

    scored_trace = [
        {
            "score": s,
            "tag": el.tag,
            "role": el.role or el.semantic_role,
            "text": el.text[:80],
            "aria": el.aria[:80],
            "label": el.label[:80],
            "id": el.el_id,
            "name": el.name,
            "scope": el.scope,
            "selector": el.best_selector or "",
        }
        for s, el in scored[:12]
    ]

    return PerceptionMatch(
        element=top_el,
        selector=selector,
        score=top_score,
        confidence=confidence,
        alternative_count=above_threshold - 1,
        scored_elements=scored_trace,
    )
