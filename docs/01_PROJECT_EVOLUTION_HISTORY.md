# Document 1: Project Evolution History — The Story

> **Project:** Tekno-Phantom-Agent  
> **Period:** March 2026 – June 2026 (~3 months)  
> **Purpose:** This document answers *why the system looks the way it does today*. Every major addition is recorded as: what triggered it, what was built, what it solved, and what new complexity or side effect it created.

---

## The Central Theme

This project started with one assumption:

> *"An LLM can generate a browser action plan. A Playwright executor can run that plan. Done."*

What actually happened is that this assumption broke in a different place every few weeks. Each break triggered a fix. Each fix added a layer. After five months, the executor has nine distinct decision paths before it attempts a single browser action. This document is the story of how each one got there.

---

## Evolution Table

| Phase | Date | Trigger / Requirement | What Was Built | Benefit Gained | Cost / Side Effect Introduced |
|---|---|---|---|---|---|
| 0 | Mar 4 | MVP: user submits task, agent executes steps | Basic executor, brain service, Next.js UI, SQLite run store | End-to-end pipeline works | Selectors from LLM are brittle — breaks on any DOM change |
| 1a | Mar 5 | First real app tested: VitaOne (form builder) | Drag-and-drop step type, 3-strategy drag cascade | Can interact with drag UI builders | Drag is timing-sensitive; 3 strategies add branching complexity |
| 1b | Mar 11–12 | Drag still failing on VitaOne; dropdown selection broken | Improved drag JS dispatch, `select_option` text-match fallback, retry logic | More reliable on complex SPAs | Execution path for drag now has 6+ branches |
| 2a | Mar 17 | No visibility into what happened after a run | HTML report generator (`report_builder.py`) | Stakeholders can read pass/fail without looking at logs | Reports are static snapshots — no interactivity |
| 2b | Mar 23 | Selectors generated at plan time break on re-run (IDs change) | `SelectorMemoryStore` — learns successful selectors per domain/step | Self-healing: previously broken selectors remembered | Only helps on *known* selectors. New pages/elements still fail from scratch |
| 2c | Mar 23 | Recovery recipes needed beyond just selector learning | `RecoveryRecipeStore` — full action sequences stored for replay | Entire multi-action recovery can be replayed automatically | Recipe matching logic is fragile — wrong recipe matched = wrong action replayed |
| 2d | Mar 23 | Need to run test cases repeatedly without rebuilding | `TestCaseStore`, `SuiteExecutor` — named reusable test cases, batch execution | Test cases persist; suites run batches | Two new state machines (test case + suite) to maintain alongside run state |
| 3a | Mar 27 | Multiple users needed; admin/user distinction | JWT auth with cookies, CSRF protection, rate limiting | Secure multi-user access | Added ~7 new files and ~500 lines. Auth bugs can lock out users entirely |
| 3b | Apr 11 | Project needs to run on a server (not just local) | Docker files per service, Nginx reverse proxy, GitHub Actions CI | Deployable to cloud; CI catches regressions | Docker adds a new failure surface — container networking, volume mounts |
| 3c | Apr 13–15 | Users can't see what the browser is doing | VNC-based browser viewer, `ViewerSessionManager`, per-run isolation | Real-time browser visibility during execution | Only works in Docker on Linux; Windows dev loses this feature |
| **4** | **Apr 8** | **Selectors still failing even with memory. Root problem: LLM doesn't know what's on the live page** | **Perception engine (`perception.py`): observe live DOM → score all elements → derive selector from match** | **Fundamental fix: selector derived from real page state, not LLM assumption** | **Added full DOM snapshot + scoring on every step. Latency increased. Complex scoring logic is hard to debug** |
| 5a | Apr 21 | Perception sometimes can't find an element; run dies | Selector recovery UI: run pauses, user provides selector manually | Human can unblock stuck runs without restart | New run status (`waiting_for_input`), timeout mechanics, UI panel to build |
| 5b | Apr 29 | Perception scoring needs more semantic signal | `intent_builder.py`: structured `StepIntent` (element type, accessible name, ordinal, scope) | More precise element matching, fewer false positives | Intent structure must be maintained for every step type — new source of inconsistency |
| 5c | Apr 29 | CSS selector manipulation scattered everywhere | `selectors/utils.py` — centralized selector utilities | Reusable, testable selector logic | Additional abstraction layer |
| 6a | May 11 | Password fields incorrectly matched to username fields on login pages | `infer_type_from_selector()`, `infer_type_from_tag()` — type inference for input fields | Login flows no longer confused by sibling inputs | Special-casing; tight coupling between selector patterns and field semantics |
| 6b | May 13 | Steps with full ARIA context still going through brittle CSS fallback | Semantic routing: steps with strong `SemanticTarget` bypass selector lookup entirely | ARIA-rich pages work without any CSS knowledge | Steps without semantic contracts get no benefit; two-tier system now |
| 6c | May 15 | `SemanticTarget` field names ambiguous (`kind`, `role`, `text`, `label` all overlapping) | `SemanticTarget` v2: renamed to `semantic_name`, `expected_role`, `accessible_name`, `scope`. Legacy kept | Clearer contract; LLM produces more consistent output | Legacy + new fields both kept → `to_canonical()` needed → backward-compat debt |
| 6d | May 17 | Checkbox clicks toggling wrong direction | State-aware checkbox: read current state before clicking | No double-toggle bugs | Extra DOM read per checkbox step |
| **7** | **May 21** | **Perception + semantic routing still can't recover from truly unknown page state. Run fails, no path forward** | **Full HITL: run pauses, user takes over in live browser, JS recorder captures interactions, run resumes** | **Zero-failure guarantee: human is the final fallback** | **Most complex feature: run state machine, browser session ownership, JS capture, interaction normalization, replay** |
| 8 | May 26 | HITL interactions captured but replay is ad-hoc | `HitlReplayer`: normalize raw JS events → structured steps → replay pipeline | Recorded HITL sessions become automated future runs | Replay correctness depends on recorded event fidelity — shadow DOM not captured |
| 9 | Jun 4 | Recovery recipes match by element key only — wrong recipe selected on similar but different pages | `recovery/snapshot.py`: `FailureContext` (URL path + page state) + `diff_recovery_snapshots()` | Context-aware recipe matching — right recipe on right page | More recipe metadata stored; matching logic more complex |

---

## Phase Narratives

### Phase 0 — The Assumption (March 4, 2026)

**What was assumed:** Build a three-tier system. Frontend sends steps. Backend uses an LLM to build a plan. Playwright runs each step by its CSS selector.

**What was built:**
- `AgentExecutor` processes steps sequentially
- `brain/` service selects LLM (OpenAI or local vLLM), executes prompts
- `frontend/` allows step authoring and run monitoring
- SQLite stores run state

**What worked:** On simple, static pages with stable IDs (e.g., `#email`, `#submit`), this worked end-to-end.

**What broke immediately:** On any page using dynamic class names, React-generated IDs, or re-rendered DOM, the LLM-generated selectors failed on the second run of the same test.

> *The LLM generates selectors at plan time. The DOM exists at execution time. These two moments rarely agree.*

---

### Phase 1 — First Real App (March 5–12, 2026)

**Trigger:** First actual test target was VitaOne — a form builder. Users drag question types from a palette onto a canvas. Nothing in Phase 0 could handle this.

**What was built:**
- `DragStep` schema (`source_selector`, `target_selector`)
- Three-strategy drag cascade in `browser_client.py`:
  1. Native `drag_and_drop()` — fast, works on ~60% of apps
  2. Manual mouse events (`mousedown → mousemove → mouseup`) — works on apps not using HTML5 drag API
  3. JavaScript `DataTransfer` injection — works on React DnD and similar frameworks
- `_expand_drag_steps()` — auto-inserts a pre-click and post-wait around every drag

**What this introduced:** The first branching execution path. A single `drag` step now executes up to three completely different code paths. Debugging a failed drag requires knowing which strategy ran.

---

### Phase 2 — The Memory Approach (March 23, 2026)

**Trigger:** The same test case failed on Monday, worked Thursday after a minor DOM change was reverted. This showed selectors were the problem, not the test logic.

**Original hypothesis:** *If we remember which selectors worked before, we can retry them on future runs.*

**What was built:**
- `SelectorMemoryStore` (SQLite) — stores `(domain, step_type, element_key) → [selector1, selector2...]` ranked by success frequency
- `RecoveryRecipeStore` (SQLite) — stores full `RecoveryRecipe` objects: domain, step_type, element_key, failure_context, list of `RecoveryAction`s to replay

**What worked:** On pages where only minor DOM changes happened between runs, memory candidates significantly improved success rate.

**What broke:** Memory only helps with known elements. First time on a new page, first time after a major redesign — memory is empty and the original brittle path runs anyway. The system had a memory it couldn't use for new failures.

---

### Phase 3 — Infrastructure (March 27 – April 11, 2026)

**Trigger:** Project needed to go from local development to something others could use and deploy.

**What was built:** Auth system (JWT, cookies, CSRF, rate limiting), Docker files for all three services, Nginx reverse proxy, GitHub Actions CI, browser viewer via VNC.

**Effect on execution architecture:** None directly. But the viewer requirement made browser session management more complex — each run now needs an associated viewer session lifecycle.

**Cost introduced:** Docker added a new failure surface that had nothing to do with execution logic. Browser viewer only works on Linux (Xvfb), not Windows development machines.

---

### Phase 4 — The Architectural Pivot (April 8, 2026)

**This was the most important change in the project.**

**What broke:** Despite selector memory, despite retry logic, selector failures remained the #1 cause of run failures. The memory approach was a band-aid on the root problem.

**Root cause identified:** The executor was using selectors as the *primary key* to find elements. A selector is a text string generated by an LLM at plan time. It has no awareness of the live DOM at execution time. Any mismatch between the LLM's assumption and the real page structure causes failure.

**The insight:** *Selectors should be derived FROM the element, not used to find it.*

**What was built — `perception.py`:**

1. Before executing any step, call `inspect_page()` to get a full DOM/ARIA snapshot
2. Parse every interactive element into an `IndexedElement` (tag, role, text, ARIA label, ID, testid, placeholder, visible state, derived selectors)
3. Score every element against the step's intent text using weighted signals (text match, role match, ARIA label match, placeholder match)
4. Confidence levels: `unique` (only one element matches well), `high` (strong match, one clear winner), `medium`, `ambiguous`
5. If `unique` or `high`: execute immediately using the derived selector — no LLM selector needed
6. If `medium` or `ambiguous`: fall back to previous pipeline

**What this solved:** Most selector failures on well-structured pages disappeared. The executor no longer depends on the LLM having guessed the right CSS selector.

**What this introduced:**
- Every step now requires a full DOM snapshot before execution — latency increase
- Scoring logic (150+ lines in perception.py) is complex and hard to tune
- Perception only works when elements have visible text, ARIA labels, or stable attributes. Pages with icon-only buttons, unlabeled inputs, or canvas rendering are still blind spots

---

### Phase 5 — Grounding and Recovery UI (April 21–29, 2026)

**Trigger:** Perception improved reliability but still left ~15-20% of steps failing on poorly-labeled apps. Users needed a way to unblock these without restarting the entire run.

**What was built:**
- Selector recovery endpoint: `POST /api/runs/{run_id}/steps/{step_id}/selector`
- Run pauses at `waiting_for_input` when all automatic recovery attempts are exhausted
- UI shows a "provide selector" panel — user inspects the page, provides the correct CSS selector
- That selector is remembered in `SelectorMemoryStore` and saved as a `RecoveryRecipe` for future runs
- `intent_builder.py` — structured `StepIntent` to give perception more signal per step

**What this solved:** Stuck runs could be unblocked in under a minute. The fix persists for future runs.

**What this introduced:** A new run status (`waiting_for_input`). A 120-second timeout before the paused run auto-cancels. UI complexity for the recovery panel.

---

### Phase 6 — Semantic Routing (May 11–17, 2026)

**Trigger:** Login flows were still failing on certain apps despite perception, because the password field and username field shared similar visible attributes. Perception would pick the wrong one.

**What was built:**
- `infer_type_from_selector()` — detect `type=password` from selector patterns
- `step_has_semantic_contract()` — detect if a step has enough semantic info to route directly
- Semantic routing in executor: if `semantic_name + expected_role + accessible_name` are all set, bypass CSS-based matching and go straight to `find_best_match_for_target()` (ARIA-only matching)
- `SemanticTarget` v2 with cleaner field names (`semantic_name`, `expected_role`, `accessible_name`, `scope`)

**What this solved:** Login forms, registration forms, and any page where the LLM can describe elements semantically became much more reliable.

**What this introduced:** Two-tier execution — steps with semantic contracts take a different path than steps without. Testing must cover both paths. Backward-compatible legacy fields (`kind`, `role`, `text`, `label`) are still supported, adding schema complexity.

---

### Phase 7 — Full HITL (May 21–26, 2026)

**This was the second most architecturally significant change.**

**Trigger:** Some steps fail not because of bad selectors or perception gaps, but because of genuine page complexity: CAPTCHA, multi-factor auth, custom drag widgets, file pickers. No automated system can handle these.

**The original design rejected mid-run human intervention** because it required keeping a browser session alive indefinitely while waiting for a human.

**What was built:**
- Run transitions to `waiting_for_input` with `user_input_kind = "recovery_confirm"`
- The live browser stays open (viewer session kept alive)
- A JavaScript recorder is injected into the page: captures every click, type, and select into a buffer
- UI shows a "Take over" panel — user performs the blocked action manually in the browser
- User clicks "I'm done — Resume"
- `POST /api/runs/{run_id}/steps/{step_id}/recovery-confirm` reads the recorded interactions
- `HitlManager.read_interactions()` returns the raw JS events
- `HitlReplayer.normalize_interactions()` converts them to structured steps
- Step marked `human_recovered`, run resumes from next step
- Interaction sequence saved to `RecoveryRecipeStore` for future automatic replay

**What this solved:** Runs that previously could only fail can now be recovered by a human. This is the "last resort" layer that ensures no run is permanently stuck.

**What this introduced:** The most complex state management in the project. The executor must:
- Track whether a browser session should be kept alive
- Manage a JS recorder lifecycle (start/stop/read)
- Poll for interactions without blocking the event loop
- Handle timeout if the human never responds
- Normalize diverse interaction events (different apps emit different JS events) into a consistent step representation

---

### Phase 8 — Replay Logic (June 4, 2026)

**Trigger:** HITL interactions were captured but the replay was ad-hoc — a raw event list was replayed literally, even when the page state had changed between the original session and the replay run.

**What was built:**
- `recovery/snapshot.py` — compare page state at failure time vs. now
- `FailureContext` — encode page conditions under which the original recovery was performed
- `build_failure_context_from_summary()` — reconstruct failure context from stored summary text
- `diff_recovery_snapshots()` — detect meaningful page state differences between original capture and replay attempt
- `promote_interactions_to_actions()` — promote raw JS events to structured `RecoveryAction` objects for the recipe store
- Recipe matching now checks `FailureContext` similarity, not just element key

**What this solved:** Replay accuracy improved — the system no longer tries to replay a recipe when the page conditions don't match the original session.

**What this introduced:** More metadata stored per recipe. Matching logic more complex (now uses URL path + visible elements + error text, not just element key).

---

## The Compounding Problem

Each phase solved a real failure mode. But each phase also added decision branches inside the executor:

```
Step received
    │
    ├─ [recipe store hit?] → try recipe replay  ─────────────────────────────► success
    │                                ↓ fail
    ├─ [semantic contract?] → semantic routing → find_best_match_for_target ──► success
    │                                ↓ fail
    ├─ [perception high confidence?] → use derived selector ──────────────────► success
    │                                ↓ fail
    ├─ [selector memory candidates?] → try historical selectors ──────────────► success
    │                                ↓ fail
    ├─ [original LLM selector works?] → execute directly ───────────────────► success
    │                                ↓ fail
    ├─ [recovery retry budget remaining?] → retry with delay ────────────────► success
    │                                ↓ fail
    └─ [HITL enabled?] → pause run, wait for human ─────────────────────────► human_recovered
                                     ↓ timeout
                                   FAIL
```

**This is the honest picture of what the executor does before any browser action runs.**

The architecture became this way not from over-engineering but from each layer being a direct response to a failure that the previous layer could not handle.

---

## What Was Never Revisited

The following are early decisions that survived all nine phases unchanged:

| Decision | Made In | Never Revisited Because |
|---|---|---|
| Steps execute sequentially (no parallelism) | Phase 0 | Works for single-browser tests; parallel would require session isolation |
| LLM-generated plan is the source of truth | Phase 0 | Perception reduced reliance but plan is still the starting point |
| SQLite for all stores | Phase 0 | Adequate for single-user; would need replacement for horizontal scaling |
| `mock` browser mode retained | Phase 0 | Still used for fast unit tests without a real browser |
| Artifact files written to local disk | Phase 0 | Works for single-instance; breaks with multiple replicas |
