# Document 3: Problems, Lessons & Technical Debt

> **Purpose:** The most important document. Every major failure, what was tried, what worked, what didn't, and what remains open.
> **Key section:** Evolution of Execution Architecture — the central story of this project.

---

## Evolution of Execution Architecture

This is the most important section because it explains why the executor looks the way it does today.

### Original Assumption

> "The LLM generates a CSS selector. Playwright uses that selector to find the element. Done."

Phase 0 executor was ~200 lines. One function. Find element by selector, run action, catch exception.

### What Happened

**Run 1 on a new app:** Works. The LLM guessed `#email`, `#password`, `button[type=submit]`. These matched.

**Run 2 on the same app:** Fails. The page framework regenerated the IDs. Now they are `input-2847`, `input-2848`, `btn-9201`. The LLM has no way to know this because it generates selectors from the task description, not from the live page.

This wasn't a one-off. It was every app that used a modern JS framework. The failure rate was 40-60% on real applications.

### What Was Added (in order)

| Addition | Goal | Did It Work? |
|---|---|---|
| Selector memory | Remember past successful selectors | Partially — only helps on known elements |
| Confidence scoring | Score multiple selector candidates, pick best | Partially — improved but false positives remained |
| Validation layer | Reject obviously wrong plans before execution | Partially — catches empty plans, not bad selectors |
| Perception engine | Observe live DOM, derive selector from match | Yes — this was the real fix |
| ARIA semantic routing | Skip CSS for elements with ARIA contracts | Yes — highly reliable on labeled apps |
| Recovery recipes | Replay proven action sequences | Yes — works well when context matches |
| HITL recovery | Human takes over when all else fails | Yes — guaranteed unblock path |
| Replay logic | Automate HITL sessions into future runs | Mostly — limited by recorder coverage |

### Unexpected Outcome

Each layer was added to fix a failure. Each layer also added:
- Another conditional branch in the executor
- Another configuration setting (7+ new settings per phase)
- Another state to track and debug
- Another failure mode if the layer itself misbehaved

By Phase 7, the executor had 7 distinct paths before touching the browser. A step failure could mean any one of those paths went wrong. Debugging required knowing which path ran, which required reading logs at a level of detail that isn't exposed to the user.

**The architecture did not become complex by accident. It became complex because each failure mode was real and needed a solution.**

### Recent Discovery (June 2026)

Many failures that appear to be execution failures are actually **perception coverage failures**.

- Perception works by reading ARIA labels, visible text, placeholder text, and element IDs
- Pages where buttons are icons-only (no text, no aria-label), inputs have no label, or content is rendered in canvas — perception is blind to all of these
- When perception is blind, all other layers (semantic routing, recipe replay) are also blind because they all depend on perception's ability to identify the element

**The root problem in 2026 is no longer "wrong selectors." It is "perception cannot see certain elements."**

This reframes the next phase of work: instead of adding more execution recovery layers, the focus should be on expanding perception coverage (screenshot-based identification, shadow DOM traversal, iframe support).

---

## Problem #1: Wrong Element Selection

**Status:** Partially solved  
**Introduced:** Phase 0 (March 4)  
**Fully addressed:** Phase 4 (April 8, perception engine)

### Symptoms

- Agent clicks the wrong button (e.g., clicks "Cancel" instead of "Submit")
- Agent types in the wrong field (e.g., types password into username field)
- Agent verifies the wrong text
- Steps pass but the actual UI action was incorrect

### Root Cause

The LLM generates CSS selectors from a text description of the task. It has no visibility into the live DOM. Selector quality depends entirely on how predictable the DOM structure is:

- **Predictable DOM (good):** `#email`, `[name="password"]`, `[data-testid="submit-btn"]` — stable, unique, framework-independent
- **Unpredictable DOM (bad):** `.MuiButtonBase-root-2847`, `div[class*="styles__container"]`, dynamically generated IDs — generated at render time, change between page loads

### Attempted Fixes

**Fix 1 — Confidence scoring (Phase 2)**
- Rank multiple selector candidates by specificity
- Result: Improved but didn't fix the root issue. Ranking between wrong selectors still picks a wrong selector.

**Fix 2 — Selector memory (Phase 2)**
- Remember which selector worked in the past for each element
- Result: Works on re-runs of the same app. Useless on first run or after redesigns.

**Fix 3 — Perception engine (Phase 4) — effective**
- Before executing, snapshot the live DOM
- Score every element against the step's semantic intent
- Derive the selector FROM the matched element instead of trusting the LLM
- Result: Fixed the majority of cases on apps with good ARIA labeling

### Remaining Gap

- **Icon-only buttons** (no text, no aria-label): perception gives them a score of near-zero. Executor falls back to LLM selector. This often fails.
- **Dynamically rendered content** (lazy-loaded, after animation): element may not be in the snapshot when it's taken
- **Canvas/WebGL interfaces**: no DOM elements to index
- **Shadow DOM**: `inspect_page()` currently doesn't traverse shadow roots

---

## Problem #2: Drag-and-Drop Failures

**Status:** Mostly solved, edge cases remain  
**Introduced:** Phase 1 (March 5)  
**Last worked on:** March 12, 2026

### Symptoms

- Drag completes without error but item doesn't move
- Item moves to wrong location
- Page state corrupted after drag (element stuck in drag state)

### Root Cause

There is no single "drag" API. Different JavaScript drag libraries handle events differently:

| Library | Mechanism | Playwright Native Works? |
|---|---|---|
| HTML5 native drag | `dragstart`, `dragover`, `drop` events | Partially |
| React DnD | Uses `MouseEvent` + `DataTransfer` simulation | No |
| SortableJS | Uses `touchstart` / `mousedown` | Partially |
| Custom implementations | Varies wildly | Depends |

### Attempted Fixes

**Fix 1 — Native Playwright `drag_and_drop()`**
- Simple API, handles coordinate calculation
- Works on ~60% of apps
- Fails silently on custom drag implementations

**Fix 2 — Manual mouse event sequence**
- `mousedown` on source → multiple `mousemove` steps → `mouseup` on target
- Works on apps using MouseEvent-based drag
- Timing-sensitive: too fast = no drag registered; too slow = browser drops the state

**Fix 3 — JavaScript DataTransfer injection (current default)**
- Directly dispatch `dragstart`, `dragenter`, `dragover`, `drop` events via JavaScript
- Most reliable across frameworks
- Doesn't work on apps that specifically check for `isTrusted` event flag (they reject synthetic events)

**Current state:** Three-strategy cascade. First success wins. Debug log written to `data/drag_debug.jsonl` on every drag attempt.

### Remaining Gap

- Apps checking `event.isTrusted === true` cannot be automated with any current strategy
- Drag into specific positions (reordering, not just drop zones) requires coordinate-aware targeting not currently implemented
- File drag-and-drop (drag file from desktop into browser) is not supported

---

## Problem #3: Plan Quality — LLM Generates Bad Steps

**Status:** Managed, not solved  
**Introduced:** Phase 0  
**Last worked on:** May 2026 (ongoing)

### Symptoms

- Steps are merged (two separate actions become one step, causing a miss)
- Extra steps added that weren't requested
- Generic selectors like `body`, `h1` that always match but verify nothing
- Verify step references placeholder text ("Example Domain") on a non-example.com site
- Steps out of order (verify before the action that produces the result)
- Steps use comma-separated fallback selector lists which the executor can't handle

### Root Cause

The LLM operates on text only. It cannot verify that its output is executable. Plan quality varies with:
- Model capability
- Prompt quality
- Task complexity
- Whether the task mentions specific element names vs. generic descriptions

### Attempted Fixes

**Fix 1 — Structured parser (`instruction_parser.py`)**: Rule-based, no LLM. For simple, structured task descriptions. Bypasses LLM entirely. Does not handle complex/ambiguous tasks.

**Fix 2 — `human_steps` pre-expansion**: Before calling the planner, expand the raw prompt into plain-English action lines via a separate LLM call. Ensures every intended action has an explicit line before the planner converts them to JSON. Prevents the planner from "merging" two actions.

**Fix 3 — `_sanitize_plan_steps()`**: Post-processing. Drops steps with empty selectors, generic click targets (`body`, `h1`), placeholder verify text, exact duplicates. Does not add missing steps.

**Fix 4 — `_validate_generated_plan()`**: Hard rejects plans with zero runnable steps. Soft warns on detected anomalies (login flow with no password step, drag mentioned but no drag step). Validation only rejects; it cannot fix.

**Fix 5 — Two-attempt planning loop**: If the first plan fails validation, the second attempt includes the validation feedback in the prompt as a correction signal.

**Fix 6 — Planner prompt constraints**: Explicit rules added to the system prompt:
- "Never use comma-separated selector lists"
- "Never use generic selectors like body or h1 for verification"
- "Use only Playwright-compatible selectors"
- "Cover every explicit instruction in order"

### Remaining Gap

- There is no feedback loop between execution failures and plan quality. A step that fails due to a bad plan looks identical to a step that fails due to a bad selector. The planner never learns from failures.
- Complex multi-page flows (15+ steps) still have ~20% plan dropout rate where steps are merged or dropped
- Verification steps continue to be the weakest area — the LLM often verifies the wrong element

---

## Problem #4: HITL Architecture Complexity

**Status:** Working, but fragile  
**Introduced:** Phase 7 (May 21, 2026)

### Symptoms

- Difficult to reason about which state the run is in during HITL
- JS recorder misses events on complex apps (shadow DOM, iframes, custom events)
- Replay of recorded interactions sometimes does the wrong thing if the page changed between sessions

### Root Cause

HITL requires coordinating four completely different systems simultaneously:
1. **Run state machine** — which step is active, what status transitions are allowed
2. **Browser session** — must stay alive while the run is paused; normal cleanup must be suppressed
3. **JS event recorder** — injected into a third-party page; reliability depends on page behavior
4. **Replay engine** — must convert diverse raw events to structured steps with enough context to work on a future run

No single one of these is especially complex. Combined, they create a brittle chain where a failure in any link breaks the whole flow silently.

### Attempted Fixes

**Fix 1 — JS recorder as non-invasive observer**: Recorder uses passive event listeners to avoid interfering with the page's own event handlers. Reduces interference but also reduces capture reliability.

**Fix 2 — 3-second polling with 5-second stabilization**: Interactions are polled every 3 seconds; replay is triggered only after 5 seconds of inactivity. Reduces false "I'm done" triggers.

**Fix 3 — `diff_recovery_snapshots()`**: Compare page state at capture time vs. replay time. Abort replay if the page looks significantly different. Prevents replaying on the wrong page state.

### Remaining Gap

- Shadow DOM events not captured (recorder is attached to the document root, not shadow roots)
- Iframe interactions not captured
- File picker, drag-and-drop interactions not captured
- Replay accuracy is ~85% — 15% of replays fail because the page rendered differently on the second run

---

## Problem #5: Windows Development Environment

**Status:** Worked around, not solved  
**Introduced:** Phase 0  
**Still present:** June 2026

### Symptoms

- Browser viewer (VNC) not available on Windows
- Asyncio loop policy must be explicitly set for Playwright on Windows
- Path separators cause subtle issues in some artifact code paths

### Root Cause

The full stack is designed for Linux deployment:
- Xvfb (virtual framebuffer) is Linux-only
- x11vnc is Linux-only
- Playwright's subprocess handling uses Proactor event loop on Windows, not the default Selector loop

### Workarounds

- `asyncio.WindowsProactorEventLoopPolicy()` set in `main.py` at startup
- `dev.bat` script handles Windows-specific startup order
- Docker + WSL2 for full feature set including viewer
- Viewer feature simply disabled on Windows — `BROWSER_VIEWER_ENABLED=false` is the default

### Remaining Gap

No plans to fix — Docker is the deployment target and Windows dev is treated as a reduced-feature mode. This is an accepted limitation.

---

## Problem #6: Execution Is Hard to Debug

**Status:** Partially addressed, ongoing  
**Introduced:** Phase 4 (when multiple execution paths were introduced)

### Symptoms

- A step fails but the log doesn't say which of the 7 execution paths ran
- Perception scores are not visible in the run result
- It's unclear why a recipe matched or didn't match
- The same step behaves differently on consecutive runs with no apparent reason

### Root Cause

As layers were added (Phase 4–8), the executor became a deep decision tree. Each decision is logged at DEBUG level but this level is too verbose for production and not surfaced to the user.

### Attempted Fixes

**Fix 1 — Step trace logging**: Every step logs which execution path was taken (recipe replay / semantic routing / perception / memory / direct). This is in the step log file (`step-XXX.log`).

**Fix 2 — `step_source_map`**: Maps each generated action back to its source instruction line. Logged at INFO level for traceability.

**Fix 3 — Perception candidate list in step trace**: Top-3 scored elements are written to the step trace so the matching decision can be reviewed.

**Fix 4 — `drag_debug.jsonl`**: All drag attempts log the strategy used, coordinates, success/failure per strategy.

### Remaining Gap

- Perception scores are not shown in the UI — users can't see why a step matched or didn't
- Recipe matching scores are not logged at INFO level — hard to tell which recipe was selected
- There is no single "execution trace" per step visible from the UI — only the artifacts directory

---

## Problem #7: Checkbox and State-Dependent Interactions

**Status:** Solved for checkboxes (May 17, 2026)  
**Similar issues may exist for:** Radio buttons, toggle switches, multi-select dropdowns

### Symptoms

- Checkbox test goes from checked → unchecked when the intent was to check it
- Re-running the same test produces opposite results on alternating runs

### Root Cause

Browser automation `click()` is stateless — it always fires a click regardless of current element state. For checkboxes and toggles, a click changes state. If the intent is "check this box" and it's already checked, the correct action is no-op. `click()` would incorrectly uncheck it.

### Fix

Executor reads current `checked` state from the element before clicking. If the desired end state matches current state, skip the click entirely.

### Remaining Gap

- Radio buttons: clicking an already-selected radio button is harmless (it stays selected), but multi-step forms may break if a radio deselects another option unexpectedly
- Toggle switches with custom ARIA roles: `checked` state detection may not work if the toggle uses `aria-pressed` instead of `checked`

---

## Technical Debt Register

These are known issues that exist today and are not currently being addressed.

| Debt | Location | Impact | Age |
|---|---|---|---|
| `SemanticTarget` has both v1 and v2 fields, `to_canonical()` bridges them | `schemas.py` | Schema confusion for new contributors | Phase 6 |
| `main.py` is ~1,800 lines — contains plan utilities that belong in runtime | `main.py` | Hard to navigate; plan logic not testable in isolation | Phase 0 |
| Run artifacts written to local disk — breaks with multiple backend instances | `executor.py`, `filesystem_client.py` | Scaling blocker | Phase 0 |
| SQLite for all stores — no connection pooling, single-writer limit | All stores | Performance/scaling blocker | Phase 0 |
| `SelectorMemoryStore` remembers selectors but not element fingerprints consistently | `selector_memory.py` | Two stores with overlapping responsibilities | Phase 5 |
| Test coverage: executor has partial unit tests but no integration tests against a real browser | `backend/tests/` | Regressions in execution paths hard to catch | Phase 0 |
| `RecoveryRecipeStore` matching has no TTL — old recipes never expire | `recovery_recipe_store.py` | Stale recipes can incorrectly match after app redesigns | Phase 2 |
| Drag debug log grows unbounded | `data/drag_debug.jsonl` | Disk usage over time | Phase 1 |
| `selector_memory.sqlite3` can grow to thousands of entries with no pruning | `data/selector_memory.sqlite3` | Slow candidate lookups over time | Phase 2 |
| `BROWSER_MODE=mcp` exists in settings but no MCP browser client implementation | `config.py` | Dead setting, confusing | Phase 0 |
| Viewer feature references `/usr/share/novnc` — hardcoded Linux path | `config.py` | Breaks on non-standard Linux installs | Phase 3 |

---

## Lessons Learned

### 1. The LLM's role should be semantic description, not selector generation

The selector problem consumed most of the project's first three months. The root fix (perception engine, April 8) required the LLM to describe *what element* to interact with, not *which CSS selector* to use. This is the right division of labor and should have been the design from Phase 0.

**If starting over:** Design the plan schema around semantic targets (`SemanticTarget`) from day one. Let the perception engine handle selector derivation exclusively. Never expose LLM-generated CSS selectors to the executor.

### 2. Recovery layers compound complexity; they don't replace the root fix

Each recovery layer (selector memory → recipes → perception → HITL) was added to handle failures the previous layer couldn't cover. Each layer added code complexity and a new failure mode. None of them fixed the root issue until the perception engine addressed the fundamental problem.

**If starting over:** Build perception first. Add recovery layers only after the primary path is reliable.

### 3. HITL is essential, but it should be designed as a first-class feature, not a retrofit

HITL was added as a last resort in Phase 7. Because it was retrofitted, it required touching the run state machine, browser session management, JS recording, and the replay pipeline — four different systems that weren't designed to support it.

**If starting over:** Design the run state machine to support `waiting_for_input` from Phase 0. This would make the HITL implementation straightforward rather than a complex retrofit.

### 4. The execution pipeline needs a visible trace

Today, if a step fails, the user sees "element not found." The logs show which of 7 paths ran, which element scored highest, and why the recipe didn't match — but this is buried in DEBUG logs. A production-ready system needs this information surfaced in the UI or in structured step metadata.

**What to add:** A `step_execution_trace` field on `StepRuntimeState` that records: path taken, perception top-3 candidates with scores, recipe match/miss reason.

### 5. Plan generation and execution validation are not the same problem

The project invested heavily in plan generation quality (instruction_parser, `human_steps` expansion, `_sanitize_plan_steps`, `_validate_generated_plan`). These helped, but they operate at plan time on text alone. Execution validation — did the step actually do the right thing? — is a different problem that requires observing the DOM after each action.

**What to add:** Post-action observation. After clicking "Submit", take a snapshot and check: did the expected page change happen? This is not currently implemented.

### 6. Drag-and-drop reliability depends on the app, not the tool

No amount of engineering makes all drag implementations work reliably. The three-strategy cascade covers most apps, but apps that check `event.isTrusted` are permanently incompatible with synthetic automation.

**Lesson:** Before committing to testing a drag-heavy app, verify that its drag implementation is automation-compatible. Document this as a known constraint, not a bug to fix.
