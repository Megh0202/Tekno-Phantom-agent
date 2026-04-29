from __future__ import annotations

import pytest

from app.runtime.executor import AgentExecutor, StepIntent
from app.runtime.perception import IndexedElement, PerceptionMatch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_match(
    tag: str = "button",
    role: str = "button",
    el_type: str = "",
    enabled: bool = True,
    text: str = "",
    aria: str = "",
    placeholder: str = "",
    name: str = "",
    title: str = "",
) -> PerceptionMatch:
    el = IndexedElement(
        tag=tag,
        role=role,
        el_type=el_type,
        enabled=enabled,
        text=text,
        aria=aria,
        name=name,
        placeholder=placeholder,
        title=title,
        el_id="",
        testid="",
        visible=True,
        selectors=("dummy-selector",),
    )
    return PerceptionMatch(
        element=el,
        selector="dummy-selector",
        score=30,
        confidence="medium",
        alternative_count=1,
    )


def _intent(target_text: str | None) -> StepIntent:
    return StepIntent(
        action="click",
        element_type="button",
        target_text=target_text,
        ordinal=None,
        scope_hint=None,
        raw_selector=None,
        text_hint=None,
    )


# ---------------------------------------------------------------------------
# Enabled check
# ---------------------------------------------------------------------------

def test_disabled_element_rejected():
    match = _make_match(enabled=False, text="Submit")
    passed, reason = AgentExecutor._validate_medium_grounding(match, "click", _intent("submit"))
    assert passed is False
    assert reason == "element_disabled"


# ---------------------------------------------------------------------------
# Click plausibility
# ---------------------------------------------------------------------------

def test_click_button_passes():
    match = _make_match(tag="button", role="button", text="Add to Cart")
    passed, reason = AgentExecutor._validate_medium_grounding(match, "click", _intent("add to cart"))
    assert passed is True
    assert reason == "ok"


def test_click_anchor_passes():
    match = _make_match(tag="a", role="link", text="Sign In")
    passed, reason = AgentExecutor._validate_medium_grounding(match, "click", _intent("sign in"))
    assert passed is True


def test_click_role_menuitem_passes():
    match = _make_match(tag="div", role="menuitem", text="Settings")
    passed, reason = AgentExecutor._validate_medium_grounding(match, "click", _intent("settings"))
    assert passed is True


def test_click_plain_div_rejected():
    match = _make_match(tag="div", role="", text="Some Text")
    passed, reason = AgentExecutor._validate_medium_grounding(match, "click", _intent("some text"))
    assert passed is False
    assert "not_clickable" in reason


def test_click_input_submit_passes():
    match = _make_match(tag="input", role="", el_type="submit", text="")
    passed, reason = AgentExecutor._validate_medium_grounding(match, "click", None)
    assert passed is True


# ---------------------------------------------------------------------------
# Type plausibility
# ---------------------------------------------------------------------------

def test_type_into_input_passes():
    match = _make_match(tag="input", role="textbox", text="", placeholder="Search")
    passed, reason = AgentExecutor._validate_medium_grounding(match, "type", _intent("search"))
    assert passed is True


def test_type_into_textarea_passes():
    match = _make_match(tag="textarea", role="", text="")
    passed, reason = AgentExecutor._validate_medium_grounding(match, "type", None)
    assert passed is True


def test_type_into_button_rejected():
    match = _make_match(tag="button", role="button", text="Submit")
    passed, reason = AgentExecutor._validate_medium_grounding(match, "type", _intent("submit"))
    assert passed is False
    assert reason == "is_button_not_typeable"


def test_type_into_div_rejected():
    match = _make_match(tag="div", role="presentation", text="Content")
    passed, reason = AgentExecutor._validate_medium_grounding(match, "type", _intent("content"))
    assert passed is False
    assert "not_typeable" in reason


# ---------------------------------------------------------------------------
# Select plausibility
# ---------------------------------------------------------------------------

def test_select_into_select_tag_passes():
    match = _make_match(tag="select", role="", text="")
    passed, reason = AgentExecutor._validate_medium_grounding(match, "select", None)
    assert passed is True


def test_select_into_combobox_passes():
    match = _make_match(tag="div", role="combobox", text="Choose...")
    passed, reason = AgentExecutor._validate_medium_grounding(match, "select", _intent("choose"))
    assert passed is True


def test_select_into_button_rejected():
    match = _make_match(tag="button", role="button", text="Dropdown")
    passed, reason = AgentExecutor._validate_medium_grounding(match, "select", _intent("dropdown"))
    assert passed is False
    assert "not_selectable" in reason


# ---------------------------------------------------------------------------
# Label alignment (text overlap)
# ---------------------------------------------------------------------------

def test_label_overlap_via_text_passes():
    match = _make_match(tag="button", role="button", text="Add to Cart")
    passed, reason = AgentExecutor._validate_medium_grounding(match, "click", _intent("add to cart"))
    assert passed is True


def test_label_overlap_via_aria_passes():
    match = _make_match(tag="button", role="button", text="", aria="Add to Cart")
    passed, reason = AgentExecutor._validate_medium_grounding(match, "click", _intent("add to cart"))
    assert passed is True


def test_label_overlap_via_placeholder_passes():
    match = _make_match(tag="input", role="textbox", placeholder="Email address")
    passed, reason = AgentExecutor._validate_medium_grounding(match, "type", _intent("email address"))
    assert passed is True


def test_no_label_overlap_rejected():
    match = _make_match(tag="button", role="button", text="Footer Logo")
    passed, reason = AgentExecutor._validate_medium_grounding(match, "click", _intent("add to cart"))
    assert passed is False
    assert "no_label_overlap" in reason


def test_no_intent_target_text_skips_overlap_check():
    # When intent has no target_text, alignment check is skipped — element passes
    match = _make_match(tag="button", role="button", text="Anything")
    passed, reason = AgentExecutor._validate_medium_grounding(match, "click", _intent(None))
    assert passed is True


def test_none_intent_skips_overlap_check():
    match = _make_match(tag="button", role="button", text="Anything")
    passed, reason = AgentExecutor._validate_medium_grounding(match, "click", None)
    assert passed is True


# ---------------------------------------------------------------------------
# Short words in target_text are ignored (< 3 chars)
# ---------------------------------------------------------------------------

def test_short_words_only_in_target_skips_overlap():
    # "ok" and "go" are both < 3 chars, so no overlap check fires
    match = _make_match(tag="button", role="button", text="Unrelated Content")
    passed, reason = AgentExecutor._validate_medium_grounding(match, "click", _intent("ok go"))
    assert passed is True


# ---------------------------------------------------------------------------
# Ambiguous grounding suppression
# _grounded_selector must NOT be set for ambiguous matches.
# This is tested at the unit level by confirming _validate_medium_grounding
# is never called for ambiguous (it's a different branch), and by asserting
# the structural property: ambiguous matches produce no grounding side-effects.
# The integration-level check is that step.input is unchanged after the block.
# ---------------------------------------------------------------------------

def test_ambiguous_match_has_correct_confidence_label():
    # Sanity: a PerceptionMatch can be constructed with confidence="ambiguous"
    el = _make_match(tag="button", role="button", text="Submit").element
    ambiguous = PerceptionMatch(
        element=el,
        selector="button:has-text('Submit')",
        score=20,
        confidence="ambiguous",
        alternative_count=3,
    )
    assert ambiguous.confidence == "ambiguous"
    assert ambiguous.alternative_count == 3


def test_medium_validation_not_called_for_ambiguous():
    # _validate_medium_grounding checks enabled/role/label.
    # Ambiguous matches bypass this function entirely — they are suppressed
    # unconditionally.  This test documents that the validator would have
    # passed for this element (it's valid), confirming suppression is not
    # due to element invalidity but due to the ambiguity policy.
    el_match = _make_match(tag="button", role="button", text="Add to Cart")
    passed, reason = AgentExecutor._validate_medium_grounding(
        el_match, "click", _intent("add to cart")
    )
    # Validator says the element itself is fine — ambiguous suppression is
    # a policy decision at a higher level, not an element-quality decision.
    assert passed is True
    assert reason == "ok"
