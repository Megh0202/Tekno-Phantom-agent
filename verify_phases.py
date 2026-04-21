"""
Verify Phases 1, 2, and 3 of the hybrid architecture upgrade.
Run from repo root: python verify_phases.py
"""

import sys
import os
import re
import inspect

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results = []

def check(label, condition, detail=""):
    status = PASS if condition else FAIL
    print(f"  [{status}] {label}" + (f"  →  {detail}" if detail else ""))
    results.append(condition)

print("\n=== PHASE 1: Domain-specific code removal ===")

# --- instruction_parser.py ---
try:
    with open("backend/app/runtime/instruction_parser.py") as f:
        parser_src = f.read()

    check("No {{selector.workflow_name}}", "{{selector.workflow_name}}" not in parser_src)
    check("No {{selector.form_canvas_target}}", "{{selector.form_canvas_target}}" not in parser_src)
    check("No {{selector.transition_button}}", "{{selector.transition_button}}" not in parser_src)
    check("No _enforce_workflow_navigation_sequence", "_enforce_workflow_navigation_sequence" not in parser_src)
    check("No _enforce_form_create_sequence", "_enforce_form_create_sequence" not in parser_src)
    check("No status_creation_mode", "status_creation_mode" not in parser_src)
    check("No transition_creation_mode", "transition_creation_mode" not in parser_src)
    check("Generic parsers still present (_parse_explicit_click)", "_parse_explicit_click" in parser_src)
    check("Login sequence still present (_enforce_login_sequence)", "_enforce_login_sequence" in parser_src)
    line_count = parser_src.count("\n")
    check(f"Line count reasonable (<700, was 1285)", line_count < 700, f"actual={line_count}")
except Exception as e:
    check("instruction_parser.py readable", False, str(e))

# --- main.py ---
try:
    with open("backend/app/main.py") as f:
        main_src = f.read()

    check("No _ensure_drag_step", "_ensure_drag_step" not in main_src)
    check("No _is_enterprise_prompt", "_is_enterprise_prompt" not in main_src)
    check("No is_enterprise_prompt key", '"is_enterprise_prompt"' not in main_src)
    check("No Enterprise planning requirements block", "Enterprise planning requirements" not in main_src)
except Exception as e:
    check("main.py readable", False, str(e))

# --- executor.py ---
try:
    with open("backend/app/runtime/executor.py") as f:
        exec_src = f.read()

    check("No VITAONE_SELECTOR_PROFILE", "VITAONE_SELECTOR_PROFILE" not in exec_src)
    check("No _is_vitaone_domain", "_is_vitaone_domain" not in exec_src)
    check("No amazon_search_box in DEFAULT_SELECTOR_PROFILE",
          "amazon_search_box" not in exec_src)
    check("No vitaone drag logic", "is_vitaone_domain" not in exec_src)
    check("DEFAULT_SELECTOR_PROFILE still present", "DEFAULT_SELECTOR_PROFILE" in exec_src)
except Exception as e:
    check("executor.py readable", False, str(e))


print("\n=== PHASE 2: ARIA-based selector expansion ===")

try:
    from app.runtime.executor import AgentExecutor

    # Patch a minimal executor instance without real dependencies
    instance = AgentExecutor.__new__(AgentExecutor)

    def _escape(self, text):
        return text.replace("'", "\\'")

    AgentExecutor._escape_playwright_text = _escape

    def _dedupe(self, items):
        seen = set()
        out = []
        for x in items:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    AgentExecutor._dedupe = _dedupe

    # Test click ARIA expansion
    variants_click = instance._derive_selector_variants("text=Submit", "click")
    check("text=Submit click → button:has-text",
          any("button:has-text('Submit')" in v for v in variants_click),
          f"got {len(variants_click)} variants")
    check("text=Submit click → [role='button']:has-text",
          any("[role='button']:has-text('Submit')" in v for v in variants_click))
    check("text=Submit click → [aria-label*=]",
          any("[aria-label*='Submit']" in v for v in variants_click))
    check("text=Submit click → input[type='submit']",
          any("input[type='submit']" in v for v in variants_click))

    # Test type ARIA expansion
    variants_type = instance._derive_selector_variants("text=Email", "type")
    check("text=Email type → input[aria-label=]",
          any("input[aria-label='Email']" in v for v in variants_type))
    check("text=Email type → input[placeholder=]",
          any("input[placeholder='Email']" in v for v in variants_type))
    check("text=Email type → textarea[aria-label=]",
          any("textarea[aria-label='Email']" in v for v in variants_type))

    # Test label:has-text expansion
    variants_label = instance._derive_selector_variants("label:has-text('Username') input", "type")
    check("label:has-text('Username') input → input[aria-label=]",
          any("input[aria-label='Username']" in v for v in variants_label))
    check("label:has-text('Username') input → input[placeholder*=]",
          any("input[placeholder*='Username']" in v for v in variants_label))

except Exception as e:
    check("ARIA expansion testable", False, str(e))


print("\n=== PHASE 3: Smart stabilization wait ===")

try:
    from app.runtime.executor import AgentExecutor

    # Check method exists
    check("_smart_stabilization_wait method exists",
          hasattr(AgentExecutor, "_smart_stabilization_wait"))

    src = inspect.getsource(AgentExecutor._smart_stabilization_wait)
    check("Uses domcontentloaded", "domcontentloaded" in src)
    check("Has >3000ms passthrough", "3000" in src)
    check("Has settle buffer logic", "settle_ms" in src)
    check("Has timeout fallback", "timeout fallback" in src or "timeout=budget" in src or "wait_for" in src)

    # Check routing in _dispatch_step
    dispatch_src = inspect.getsource(AgentExecutor._dispatch_step)
    check("_dispatch_step routes until=timeout → _smart_stabilization_wait",
          "_smart_stabilization_wait" in dispatch_src)

except Exception as e:
    check("Smart wait testable", False, str(e))


print("\n" + "=" * 50)
passed = sum(results)
total = len(results)
color = "\033[92m" if passed == total else "\033[91m"
print(f"{color}Results: {passed}/{total} checks passed\033[0m")
if passed < total:
    print("Some checks FAILED — review output above.")
else:
    print("All phases verified successfully.")
print()
