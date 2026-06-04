"""
Phase 6 — Executor helper methods unit test.
Tests: _extract_domain, _extract_element_key, _build_failure_context,
       _build_failure_context_from_summary, _derive_success_signals,
       _promote_interactions_to_actions
No browser, no server needed.
"""
from app.runtime.executor import AgentExecutor
from app.schemas import FailureContext, InteractionEvent

# ------------------------------------------------------------------ #
# _extract_domain
# ------------------------------------------------------------------ #
assert AgentExecutor._extract_domain("https://app.mybank.com/login") == "app.mybank.com"
assert AgentExecutor._extract_domain("http://localhost:8080/dashboard") == "localhost:8080"
assert AgentExecutor._extract_domain("https://shop.example.com/checkout?step=2") == "shop.example.com"
assert AgentExecutor._extract_domain("") == "unknown"
assert AgentExecutor._extract_domain("not-a-url") == "unknown"
print("PASS: _extract_domain")

# ------------------------------------------------------------------ #
# _extract_element_key — priority order
# ------------------------------------------------------------------ #

# 1. accessible_name wins over everything else
assert AgentExecutor._extract_element_key("click", {"accessible_name": "Login", "label": "ignored"}) == "login"

# 2. label when no accessible_name
assert AgentExecutor._extract_element_key("click", {"label": "Submit"}) == "submit"

# 3. text_hint
assert AgentExecutor._extract_element_key("click", {"text_hint": "Sign In"}) == "sign in"

# 4. target.* subfield
assert AgentExecutor._extract_element_key("click", {"target": {"accessible_name": "Cancel"}}) == "cancel"

# 5. name attribute
assert AgentExecutor._extract_element_key("type", {"name": "emailField"}) == "name:emailField"

# 6. selector fallback — strips attribute noise
key = AgentExecutor._extract_element_key("click", {"selector": "button.submit-btn"})
assert key.startswith("click:"), f"FAIL: selector fallback key={key!r}"

# 7. bare fallback — step type only
assert AgentExecutor._extract_element_key("type", {}) == "type"

print("PASS: _extract_element_key")

# ------------------------------------------------------------------ #
# _build_failure_context — from raw snapshot
# ------------------------------------------------------------------ #
snap_with_dialog = {
    "url": "https://app.mybank.com/login",
    "title": "Login Page",
    "interactive_elements": [
        {"role": "textbox", "visible": True},
        {"role": "dialog", "visible": True},
        {"role": "button", "visible": True},
        {"role": "button", "visible": False},   # not visible — should not count
    ],
}
ctx = AgentExecutor._build_failure_context(snap_with_dialog)
assert ctx.url_path == "/login", f"FAIL: url_path={ctx.url_path}"
assert ctx.page_title == "Login Page", f"FAIL: title={ctx.page_title}"
assert ctx.has_dialog is True, "FAIL: has_dialog should be True"
assert ctx.interactive_count == 3, f"FAIL: interactive_count={ctx.interactive_count} (expected 3 visible)"
print("PASS: _build_failure_context (with dialog)")

snap_no_dialog = {
    "url": "https://app.mybank.com/dashboard",
    "title": "Dashboard",
    "interactive_elements": [
        {"role": "button", "visible": True},
        {"role": "link", "visible": True},
    ],
}
ctx2 = AgentExecutor._build_failure_context(snap_no_dialog)
assert ctx2.has_dialog is False, "FAIL: has_dialog should be False"
assert ctx2.interactive_count == 2
print("PASS: _build_failure_context (no dialog)")

ctx_none = AgentExecutor._build_failure_context(None)
assert ctx_none.url_path == ""
assert ctx_none.has_dialog is False
print("PASS: _build_failure_context (None snapshot returns empty FailureContext)")

# ------------------------------------------------------------------ #
# _build_failure_context_from_summary — from summarized snapshot
# ------------------------------------------------------------------ #
summary = {
    "url": "https://app.mybank.com/checkout",
    "title": "Checkout",
    "visible_interactive_count": 7,
    "interactive_sample": [
        {"role": "textbox", "visible": True},
        {"role": "alertdialog", "visible": True},
    ],
}
ctx3 = AgentExecutor._build_failure_context_from_summary(summary)
assert ctx3.url_path == "/checkout", f"FAIL: url_path={ctx3.url_path}"
assert ctx3.has_dialog is True, "FAIL: alertdialog not detected"
assert ctx3.interactive_count == 7, f"FAIL: interactive_count={ctx3.interactive_count}"
print("PASS: _build_failure_context_from_summary")

# ------------------------------------------------------------------ #
# _derive_success_signals
# ------------------------------------------------------------------ #

# URL changed
failure_ctx = FailureContext(url_path="/login", has_dialog=False, interactive_count=3)
after_snap = {
    "url": "https://app.mybank.com/dashboard",
    "interactive_elements": [],
}
sigs = AgentExecutor._derive_success_signals(failure_ctx, after_snap)
assert sigs.url_path_changed is True, "FAIL: url_path_changed"
assert sigs.url_path_after == "/dashboard", f"FAIL: url_path_after={sigs.url_path_after}"
assert sigs.dialog_dismissed is False
assert sigs.dialog_appeared is False
print("PASS: _derive_success_signals — URL changed")

# Dialog dismissed
failure_ctx2 = FailureContext(url_path="/app", has_dialog=True)
after_snap2 = {
    "url": "https://app.mybank.com/app",
    "interactive_elements": [],   # no dialog after
}
sigs2 = AgentExecutor._derive_success_signals(failure_ctx2, after_snap2)
assert sigs2.url_path_changed is False
assert sigs2.dialog_dismissed is True, "FAIL: dialog_dismissed"
assert sigs2.dialog_appeared is False
print("PASS: _derive_success_signals — dialog dismissed")

# Dialog appeared
failure_ctx3 = FailureContext(url_path="/app", has_dialog=False)
after_snap3 = {
    "url": "https://app.mybank.com/app",
    "interactive_elements": [{"role": "dialog", "visible": True}],
}
sigs3 = AgentExecutor._derive_success_signals(failure_ctx3, after_snap3)
assert sigs3.dialog_appeared is True, "FAIL: dialog_appeared"
assert sigs3.dialog_dismissed is False
print("PASS: _derive_success_signals — dialog appeared")

# No change at all
failure_ctx4 = FailureContext(url_path="/app", has_dialog=False)
after_snap4 = {
    "url": "https://app.mybank.com/app",
    "interactive_elements": [],
}
sigs4 = AgentExecutor._derive_success_signals(failure_ctx4, after_snap4)
assert sigs4.url_path_changed is False
assert sigs4.dialog_dismissed is False
assert sigs4.dialog_appeared is False
print("PASS: _derive_success_signals — no change")

# None snapshot
sigs5 = AgentExecutor._derive_success_signals(failure_ctx, None)
assert sigs5.url_path_changed is False
print("PASS: _derive_success_signals — None snapshot returns empty SuccessSignals")

# ------------------------------------------------------------------ #
# _promote_interactions_to_actions
# ------------------------------------------------------------------ #
events = [
    InteractionEvent(
        kind="click", tag="BUTTON", role="button",
        label="Sign In", text="Sign In",
        selector="button#login", selector_chain=["button#login"],
        timestamp_ms=1000,
    ),
    InteractionEvent(
        kind="input", tag="INPUT", label="Username",
        placeholder="Enter username", value="admin",
        selector="input[name=user]", selector_chain=["input[name=user]"],
        clear_first=True, timestamp_ms=2000,
    ),
    InteractionEvent(
        kind="change", tag="SELECT", label="Role",
        value="admin", option_text="Admin",
        selector="select[name=role]", selector_chain=["select[name=role]"],
        timestamp_ms=3000,
    ),
    InteractionEvent(
        kind="toggle", tag="INPUT", label="Remember me",
        checked=True,
        selector="input[type=checkbox]", selector_chain=["input[type=checkbox]"],
        timestamp_ms=4000,
    ),
    InteractionEvent(
        kind="navigate", tag="", url="https://app.example.com/dashboard",
        selector="", selector_chain=[], timestamp_ms=5000,
    ),
]

actions = AgentExecutor._promote_interactions_to_actions(events)
assert len(actions) == 5, f"FAIL: expected 5 actions, got {len(actions)}"

assert actions[0].action_type == "click_control", f"FAIL: {actions[0].action_type}"
assert actions[0].label == "Sign In"
assert actions[0].text == "Sign In"
assert actions[0].role == "button"
print("PASS: click → click_control, semantic fields preserved")

assert actions[1].action_type == "fill_field", f"FAIL: {actions[1].action_type}"
assert actions[1].value == "admin"
assert actions[1].label == "Username"
assert actions[1].clear_first is True
print("PASS: input → fill_field, value and clear_first preserved")

assert actions[2].action_type == "select_option", f"FAIL: {actions[2].action_type}"
assert actions[2].option_text == "Admin"
assert actions[2].value == "admin"
print("PASS: change on SELECT → select_option, option_text preserved")

assert actions[3].action_type == "click_control", f"FAIL: {actions[3].action_type}"
assert actions[3].checked is True
print("PASS: toggle → click_control, checked state preserved")

assert actions[4].action_type == "navigate_to", f"FAIL: {actions[4].action_type}"
assert actions[4].value == "https://app.example.com/dashboard"
print("PASS: navigate → navigate_to, URL in value")

print()
print("Phase 6 complete — all executor helpers verified")
