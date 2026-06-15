# Document 7: Test Coverage Map

> **Purpose:** Explain which parts of the system are tested, which are not, and why the untested parts are risky.  
> **How to use this:** Before touching any file, look up where it sits on this map. If it has no test coverage, be extra careful and test manually after the change.

---

## What Testing Means Here (Brief Orientation)

The tests in this project are all backend Python tests run with `pytest`. They don't open a real browser — they use a "mock" browser client that pretends to click things and returns fake results. This means:

- **Unit tests** verify logic in isolation: "does the selector scoring function return the right selector for this element?"
- **Integration tests** verify that pieces fit together: "does the executor correctly pause the run when all selectors fail?"
- **What's NOT tested here:** Anything that requires a real browser (Playwright), a real LLM, or a running server. Those require manual testing.

**Current test count:** 241 tests collected. 156 passing, 85 failing (pre-existing failures — these tests describe behavior that was changed but the tests were not updated). 1 test file has a broken import (`test_plan_sanitizer.py`).

---

## 1. What Each Test File Covers

### `test_executor_selector_fallback.py` — 83 tests, largest file

This is the most important test file. It tests the **selector pipeline** — the 7-layer process the executor uses to find the right element on a page when a step has a selector like `#submit` or a semantic target like "Submit button".

What these tests verify:
- If you have a selector profile (remembered selectors for `login_button`, `email`), does the executor try those first?
- If perception identifies an element from the live page, does the executor use that over the profile?
- Does the confidence gating work? (If perception says "ambiguous", does the executor not blindly click something wrong?)
- Does the HITL escalation trigger correctly when all selectors fail?
- Does intent building correctly extract "this step wants a button in the login form" from the raw step?

**Status: 23 currently failing** — these failures are because the selector pipeline was refactored and the tests were not updated to match the new method signatures. The production code works; the tests are describing old behavior.

---

### `test_validate_medium_grounding.py` — 22 tests, all failing

This file tests the **medium-confidence perception validation** — the logic that says "perception found a match with 61% confidence, but before we click it, let's verify the element tag and label actually match what the step intends."

For example: if the step says "click the Submit button" and perception matches a `<div>` element — that's suspicious. This validation catches it and rejects the match.

**Status: All 22 failing** — the validation logic was moved and refactored in `perception.py`. The tests call the old function signature. This is a critical coverage gap because medium-confidence validation is running in production but the tests are all broken.

---

### `test_auto_wait_controls.py` — 3 tests, all passing

Tests the **automatic wait injection for drag steps**. When the planner generates a drag step, the system automatically adds a small pre-click action (to ensure the drag source is focused) and a post-drop wait. These 3 tests verify that:
- The pre-click is injected by default
- The pre-click can be disabled for specific source elements (like canvas fields)
- The post-wait duration uses the correct setting

---

### `test_assess_click_select_option.py` — 8 tests, all passing

Tests the **post-click validation for select elements**. After the agent selects an option from a dropdown, the system checks whether the parent `<select>` element actually changed its value. This tests:
- Does a normal `click` on a `<select>` pass validation unconditionally?
- Does selecting an `<option>` verify the parent value changed?
- What happens when the option was already selected?

---

### `test_admin_auth.py` and `test_run_auth_ownership.py` — passing (mostly)

Tests the **JWT authentication** and **ownership isolation**. Verifies:
- The app starts correctly when auth is disabled (no secret needed)
- Admin tokens grant access to protected endpoints
- A user can only see their own runs, not another user's runs
- 2 auth tests fail because the test client isn't setting the CSRF cookie correctly (test setup issue, not a production bug)

---

### `test_plan_normalizer.py`, `test_instruction_parser.py`, `test_plan_validation.py` — mostly passing

These test the **planning pipeline**:
- `plan_normalizer`: Takes raw LLM output (which may have wrong field names, invented step types, or garbled JSON) and converts it to the canonical schema the executor expects
- `instruction_parser`: The rule-based parser that converts a structured task description into steps without calling an LLM at all
- `plan_validation`: Checks that a plan doesn't have too many steps, doesn't use unsupported step types, etc.

---

### `test_shadow_dom_traversal.py` — 3 tests, all passing (new)

Tests the **shadow DOM traversal** added to `inspect_page()`. Verifies that:
- Elements inside open shadow roots are discovered
- Nested shadow roots (shadow inside shadow) are discovered
- Closed shadow roots are correctly excluded (they're browser-inaccessible by design)
- No element is returned twice

---

## 2. The 7-Layer Pipeline — What's Tested vs Not

The executor runs 7 layers to locate an element for every click/type/select step. Here's what's covered:

```
Layer 1: Recipe replay
         "Has a human previously solved this exact failure? Replay their actions."
         STATUS: NOT TESTED
         Why it's risky: If replay incorrectly matches the wrong saved session, it could
         perform the wrong action on a different page — silently.

Layer 2: Semantic routing (fast path)
         "This step has a complete semantic target. Go straight to perception."
         STATUS: PARTIALLY TESTED
         What's tested: Classification logic (does a step get classified as fast-path?)
         What's not: Actual browser execution on the fast path

Layer 3: Perception grounding
         "Read the live page DOM, score every element, find the best match."
         STATUS: PARTIALLY TESTED
         What's tested: Confidence levels, scoring logic, selector derivation
         What's not: Real DOM matching (tests use mock snapshots)

Layer 4: Selector memory
         "Has this selector worked before on this domain? Try remembered selectors first."
         STATUS: TESTED for candidate generation, NOT TESTED for write→read cycle

Layer 5: LLM selector repair
         "Call the brain service: 'here's the failed element, suggest better selectors'."
         STATUS: NOT TESTED (brain is always mocked in tests)

Layer 6: Retry loop
         "Try each candidate selector until one works or all fail."
         STATUS: PARTIALLY TESTED (mock browser returns pre-configured results)

Layer 7: HITL pause
         "All options exhausted. Pause, keep browser open, ask a human."
         STATUS: TESTED for the transition to waiting_for_input
         NOT TESTED for the full pause → human acts → resume → recipe saves cycle
```

---

## 3. What Happens When You Run ALL the Tests

```bash
cd backend
pytest tests/ -q --ignore=tests/test_plan_sanitizer.py
```

Output today:
```
85 failed, 156 passed, 28 warnings
```

**The 85 failures are all pre-existing and fall into these categories:**

| Failure group | Count | Root cause | What it means |
|---|---|---|---|
| `test_validate_medium_grounding.py` | 22 | Perception validation API changed | Tests call old method name — not a production bug |
| `test_executor_selector_fallback.py` | 23 | Selector pipeline refactored | Tests use old mock signatures — production code is correct |
| `test_instruction_parser.py` | ~12 | Structured parser output format changed | Tests assert old format |
| `test_runs.py` | ~9 | API response shapes changed | Tests check old field names |
| `test_plan_validation.py` | ~7 | Plan schema updated | Tests check old validation rules |
| `test_step7_verification.py` | ~6 | Verification logic refactored | Tests describe old behavior |
| Auth tests | 2 | CSRF cookie not set up in test HTTP client | Test setup issue |

**Key insight:** These tests aren't catching bugs — they're describing behavior that no longer exists. Fixing them means updating the test assertions to match current behavior, not fixing the production code.

---

## 4. Highest Risk: Untested Production Paths

These paths run in production every day but have zero test coverage. A bug here could cause silent wrong behavior — no test would catch it.

### Critical

**HITL full cycle (pause → human acts → resume → recipe saves)**  
If the JS recorder misses an interaction, or the normalization produces a broken recipe, the next replay could perform the wrong actions. Nothing will catch this except a human noticing the run did something unexpected.

**Autonomous mode** (`_execute_autonomous_run`)  
When a run is in autonomous mode, the LLM generates new steps mid-run based on page state. If the LLM prompt changes, the step normalization breaks, or the page summary is incorrect — the run either stalls or takes wrong actions. Zero tests cover any of this.

**Recipe context matching**  
When replaying a saved HITL session, the system checks whether the current page looks like the page where the failure originally happened. If the matching is too loose, it replays the wrong recipe on a completely different page. Not tested.

### High

**`verify_image` baseline comparison**  
The image verification step (screenshot comparison against a saved baseline) has no tests. A regression in image loading, comparison tolerance, or baseline management would be invisible until someone manually runs a verify_image test case and sees it always pass or always fail.

**Concurrent execution lock**  
The executor has an `asyncio.Lock` per run to prevent two simultaneous execute calls from racing (can happen during HITL: the background retry fires while the main execute is still processing a failure). If this lock has a bug, a race could corrupt the run state. Never tested.

### Medium

**`navigate` step**  
The most common step type (almost every run starts with one) has no dedicated test. The mock browser's `navigate()` returns success unconditionally, so the test never exercises real navigation failure handling.

**Drag cascade (Strategy 1 → 2 → 3)**  
The `drag_debug.jsonl` file is the only observability for drag. No test verifies that all three strategies are tried in order or that the fallback actually works.

---

## 5. Safe vs. Risky Areas to Modify

**Lower risk (changing these will break tests and you'll know immediately):**
- `plan_normalizer.py` — 10 passing tests
- `viewer_session.py` — 8 passing tests
- `browser_client.py` inspect_page JS — 3 new passing tests
- Selector candidate generation in `executor.py` — 60 passing fallback tests

**Higher risk (no tests will catch a regression):**
- `hitl_manager.py` / `hitl_replayer.py` — 0 integration tests
- `recovery_recipe_store.py` — 0 tests
- The autonomous mode code in `executor.py` (`_execute_autonomous_run`) — 0 tests
- `verify_image` step handler — 0 tests
- `suite_executor.py` — 0 tests
