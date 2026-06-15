# Tekno-Phantom-Agent — Comprehensive Project Documentation

> **Created:** June 2026  
> **Project duration:** March 2026 – present (~3 months active development)  
> **Repository:** github.com/Megh0202/Tekno-Phantom-agent  
> **Team:** Primary — Megh0202 (you). Contributor — Paranjay-pandu (Docker, deployment). Collaborator — "harshini" (March sprint).

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Initial Requirements (Phase 1)](#2-initial-requirements-phase-1)
3. [Architecture](#3-architecture)
4. [Evolution Timeline](#4-evolution-timeline)
5. [Feature Log — What Was Added, Why, and Its Effect](#5-feature-log)
6. [All Modules, Classes, and Key Functions](#6-all-modules-classes-and-key-functions)
7. [API Reference](#7-api-reference)
8. [Data Models and Schemas](#8-data-models-and-schemas)
9. [Configuration Reference](#9-configuration-reference)
10. [Blockers and Challenges](#10-blockers-and-challenges)
11. [Current State (June 2026)](#11-current-state)
12. [Known Gaps and Next Steps](#12-known-gaps-and-next-steps)

---

## 1. Project Overview

**Tekno-Phantom-Agent** is a browser automation agent system that executes multi-step test scenarios on live web applications. It is designed as a QA automation tool where:

- A user describes a test scenario in natural language or as structured steps via a web UI.
- The system converts those steps into a machine-executable action plan using an LLM.
- A backend agent executes each step inside a real Playwright browser.
- Results, screenshots, and HTML reports are persisted and surfaced back in the UI.

The key design principle is **separation of concerns**:
- The **Brain** service owns all LLM logic (provider selection, prompt execution).
- The **Backend** owns all runtime orchestration (execution, state, browser control).
- The **Frontend** owns user interaction only (step authoring, run monitoring).

This means swapping the LLM (OpenAI → Anthropic → local vLLM) requires no changes to the backend or frontend.

---

## 2. Initial Requirements (Phase 1)

Established at project start (early January 2026, documented in `docs/PRD.md`).

### Core Functional Scope

| # | Requirement | Status |
|---|---|---|
| 1 | Local LLM mode via vLLM | Implemented |
| 2 | Cloud LLM mode via OpenAI | Implemented |
| 3 | Anthropic/Claude LLM mode | Added later (Phase 3) |
| 4 | Browser actions: click, select, scroll, type, wait, popup | Implemented |
| 5 | Verification: text and image | Implemented |
| 6 | Drag-and-drop action | Added in first sprint |
| 7 | File System MCP for task artifacts | Implemented (partial) |
| 8 | Next.js UI for task creation and execution | Implemented |
| 9 | Brain/Agent separation (separate services) | Implemented |

### Non-Functional Requirements

| Priority | Requirement |
|---|---|
| Determinism | Explicit action schema; predictable execution engine |
| Safety | Domain allowlist, bounded step count and timeout, cancellation |
| Observability | Run timeline, per-step status, errors, artifacts |
| Replaceability | Provider abstraction for LLM and browser/MCP tools |

### Acceptance Criteria (original Phase 1)

- User can create a run with one or more steps from the UI.
- Backend returns a `run_id` and updates status per step.
- Final run status is `completed`, `failed`, or `cancelled`.
- All core browser actions work (click, type, select, scroll, wait, handle_popup).
- Verification actions (verify_text, verify_image) produce pass/fail.
- Admin sets `LLM_MODE`; end user cannot change it.
- Each step stores: input, normalized command, status, start/end time, message, artifact refs.

---

## 3. Architecture

### Service Map

```
┌─────────────────────────────────────────────────────┐
│                   Nginx (port 80)                   │
│        reverse proxy — routes by path prefix        │
└───────┬───────────────────┬───────────────────┬─────┘
        │                   │                   │
        ▼                   ▼                   ▼
┌──────────────┐   ┌──────────────────┐   ┌──────────┐
│  Frontend    │   │    Backend       │   │  Brain   │
│  Next.js 16  │   │  FastAPI/Python  │   │  FastAPI │
│  React 19    │   │  port 8080       │   │  port 8090│
│  TypeScript  │   │                  │   │          │
└──────────────┘   └────────┬─────────┘   └────┬─────┘
                            │                   │
                    ┌───────▼────────┐          │
                    │  Playwright    │  HTTP ───►│
                    │  Browser       │          │
                    │  (Chromium)    │   LLM ───►│ OpenAI /
                    └───────┬────────┘          │ Anthropic /
                            │                   │ vLLM
                    ┌───────▼────────┐          └──────────
                    │  SQLite DBs    │
                    │  auth.db       │
                    │  run_store     │
                    │  selector_mem  │
                    └────────────────┘
```

### Layers (as originally designed)

1. **Next.js UI** — task builder, step editor, run status monitor, HITL interface
2. **FastAPI Agent API** — REST + WebSocket endpoints, request validation, auth
3. **Agent Runtime** — planner pipeline + step executor
4. **Brain Service** — LLM provider abstraction (OpenAI, Anthropic, vLLM)
5. **Adapter layer** — Browser (Playwright MCP), Filesystem (MCP)

### Execution Flow

```
User submits task (prompt or steps)
        │
        ▼
POST /api/runs  ─── validates schema ──► create RunState (pending)
        │
        ▼
background_task: executor.execute(run_id)
        │
        ├── [plan mode] POST /brain/plan_task ──► LLM generates steps JSON
        │
        ├── normalize_plan_steps() ──► canonicalize step types/fields
        │
        ├── _sanitize_plan_steps()  ──► drop invalid/duplicate steps
        │
        └── per step loop:
              │
              ├── perception: inspect_page() → build_element_index()
              ├── find_best_match() → derive selector from live DOM
              ├── execute action via Playwright MCP
              ├── if fails → selector recovery / HITL
              └── persist step result to RunStore
```

---

## 4. Evolution Timeline

### Phase 0 — Foundation (March 4–11, 2026)

**Commits:** `7f5d6db` (Initial commit) through `865ec61`

**What was built:**
- Three-service project scaffold: `backend/`, `brain/`, `frontend/`
- FastAPI skeleton with `/health`, `/api/config`, `/api/runs`, `/api/runs/{id}`, `/api/runs/{id}/cancel`
- `AgentExecutor` first version — processes steps sequentially
- `brain/` with `LLM_MODE` toggle: `local` (vLLM) or `cloud` (OpenAI)
- Next.js UI with multi-step task builder and run status monitor
- SQLite-backed run persistence (`run_store.sqlite3`)
- Artifact logging to `artifacts/<run_id>/step-XXX.log`
- `mock` browser mode (no real browser, used for testing)
- Playwright browser mode with Chromium support

**Drag-and-drop (March 5):**
- Added `DragStep` to the action schema (`source_selector`, `target_selector`)
- Implemented three-strategy drag execution in `browser_client.py`:
  1. Native Playwright `drag_and_drop()`
  2. Manual `mousedown → mousemove → mouseup` sequence
  3. `DataTransfer` injection via JavaScript
- Added `_expand_drag_steps()` — automatically pre-clicks the source element and waits after drop

**Effect:** Enabled testing of drag-and-drop form builders (e.g., VitaOne).

---

### Phase 1 — Reliability & Reports (March 12–23, 2026)

**Commits:** `9036285` through `3d8e1fc`

**VitaOne flow fixes (March 12):**
- Drag stability: switch to JS dispatch when native drag fails
- Dropdown option handling: added `select_option` with text-match fallback
- Back-navigation selectors: improved handling of browser history actions

**HTML Report (March 17):**
- Added `report_builder.py` — generates an HTML artifact (`report.html`) per run
- Report includes: run summary, per-step status (pass/fail), screenshots, timestamps, error messages

**Selector Recovery (March 23):**
- Added `SelectorMemoryStore` (SQLite-backed) — learns which selectors succeeded per domain/step
- When a step fails, the system queries the memory store for known-good selector alternatives
- Added `RecoveryRecipeStore` — stores full action recipes (not just selectors) for replay

**Suite support:**
- Added `SuiteExecutor`, `SuiteStore` — batch execution of multiple test cases
- Added `TestCaseStore` — CRUD persistence for named, reusable test cases

**Effect:** Runs became repeatable and self-healing. HTML reports enabled non-technical review.

---

### Phase 2 — Auth, Deployment, Browser Viewer (March 27–April 15, 2026)

**Commits:** `daee824` through `27472f6`

**Auth system (March 27, April 10):**
- Added JWT-based authentication (`passlib`, `python-jose`, `bcrypt`)
- `auth/` module: `service.py`, `security.py`, `dependencies.py`, `csrf.py`, `rate_limiter.py`
- SQLite `auth.db` with `User`, `RefreshToken` models
- Cookie-based sessions with HttpOnly/Secure flags
- CSRF protection on state-changing routes
- Rate limiter on auth endpoints

**Effect:** Multi-user support; UI login/logout flow. Admin vs. user permissions.

**Docker/deployment (April 11):**
- Added `Dockerfile` per service (backend, brain, frontend)
- Added `docker-compose.yml` (prod) and `docker-compose.dev.yml` (dev)
- Added Nginx config (`nginx/default.conf`) as reverse proxy
- `dev.bat` for Windows local startup
- GitHub Actions CI: `backend-ci.yml`, `frontend-ci.yml`
- Contributor Paranjay-pandu authored Docker files and deployment scripts

**Effect:** Project became deployable to cloud/VPS. CI ensures PRs don't break builds.

**Browser Viewer — first attempt (April 13–15):**
- Added `ViewerSessionManager` and `viewer_session.py`
- First implementation: shared NoVNC viewer
- Commit "No VNC" (`27472f6`) — transitioned to per-run isolated viewer sessions
- Each run gets its own VNC session so concurrent runs don't interfere

**Effect:** Users can watch the browser execute their test case in real-time from the UI.

---

### Phase 3 — Perception Engine (April 8, 2026)

**Commit:** `895e8d9` — "Implement perception-based execution and CORS/schema fixes"

This was the most architecturally significant change in the project.

**Problem:** The original executor used LLM-generated CSS selectors directly. These were brittle — a minor DOM change would break the selector and fail the step.

**Solution — perception-first execution:**
- Added `perception.py` — observes the live DOM via `inspect_page()` before each step
- Builds an `ElementIndex` from the snapshot: all interactive elements with their text, ARIA roles, labels, IDs, data-testids, selectors
- `find_best_match(intent_text, step_type, index)` scores each element against the step's semantic intent
- Only if confidence is `unique` or `high` does it execute; otherwise falls back to the original pipeline

**Key classes added:**
- `IndexedElement` — single interactive element from DOM snapshot
- `ElementIndex` — scored index of all elements on page
- `PerceptionMatch` — result with confidence level and derived selector
- `build_element_index()` — builds the index from an `inspect_page()` snapshot
- `find_best_match()` — main scoring entry point
- `find_best_match_for_target()` — scoring using full semantic `SemanticTarget` contract
- `find_by_signatures()` — re-identification using stored element fingerprints

**Effect:** Step reliability improved significantly. Selectors are now derived from observed DOM state, not assumed from generation time.

---

### Phase 4 — Element Grounding & Selector Recovery (April 21–28, 2026)

**Commits:** `9cee2e1` through `00098b4`

**Selector Recovery UI (`9cee2e1` — April 21):**
- When a step fails due to selector not found, the run pauses at `waiting_for_input`
- UI shows a "selector help" panel — user can inspect the page and provide the correct selector manually
- `POST /api/runs/{run_id}/steps/{step_id}/selector` endpoint accepts the user-provided selector
- The provided selector is remembered in `SelectorMemoryStore` for future runs
- Also saved to `RecoveryRecipeStore` for automated replay

**Element grounding (`a00c64d` — April 29):**
- Added `intent_builder.py` — builds a structured `StepIntent` from a step definition
- `StepIntent` encodes: element type, target text, accessible name, ordinal hint, scope hint
- Used to drive more precise element matching in `perception.py`
- Added `selectors/utils.py` — selector utility helpers

**Fix sign-in click (`00098b4`):**
- Specific fix for login flows: improved selector precedence for `#username`, `#password`, `input[name=password]`
- Added special handling for password fields when multiple inputs are on the page

**Effect:** HITL-assisted recovery allows a human to unblock a stuck run without restarting. The fix is persisted so the next run is automatic.

---

### Phase 5 — Semantic Routing (May 11–15, 2026)

**Commits:** `5ce2e03` (intent), `a4d8004` (pswd), `4b4af9a` (semantic routing), `b842702` (semantic 2)

**Intent system maturation:**
- `intent_builder.py` expanded with: `editable_selector_field()`, `intent_element_type()`, `intent_ordinal()`, `intent_scope_hint()`, `intent_target_text()`
- `step_has_semantic_contract()` — detects whether a step has enough semantic info to skip brittle selector lookup
- `serialize_step_intent()` — serializes intent for logging/debugging

**Semantic routing in executor:**
- Steps with a strong `SemanticTarget` (semantic_name + expected_role + accessible_name) bypass selector lookup entirely
- Route directly to `find_best_match_for_target()` which scores against ARIA properties
- Password field special-casing: `infer_type_from_selector()` detects password inputs and adds `type=password` constraint to matching

**`SemanticTarget` v2 schema (May 15):**
- Renamed fields to be clearer: `semantic_name`, `expected_role`, `accessible_name`, `scope`
- Legacy fields (`kind`, `role`, `text`, `label`, `placeholder`, `context`) kept for backward compatibility
- `to_canonical()` method resolves new-or-legacy fields to canonical dict

**Effect:** Steps with semantic contracts became highly reliable — no brittle CSS selectors needed. Login/form flows improved significantly.

---

### Phase 6 — UI Improvements (April 29–May 8, 2026)

**Commits:** `dddc359`, `bb79d5d`, `6a69dba`, `40e2122`, `97f7301`

**Step action logs (May 4):**
- Per-step execution log surfaced in the UI (`step-XXX.log` in artifacts)
- Step list shows expandable log output for each step

**"Orange" theme (May 5):**
- UI color/branding update — added orange highlight color
- Step status indicators updated with the new palette

**Verify image (May 6):**
- `verify_image` step implementation improved
- Screenshot capture and baseline comparison via Pillow
- Failure saves a `step-XXX-failed.png` artifact for visual diff review

**Result layout (May 8):**
- Redesigned run result page: cleaner step list, pass/fail summary at top
- Added step duration display and error message inline

**Checkbox (May 17):**
- Added checkbox interaction support: `click` steps on `input[type=checkbox]` now use state-aware toggling
- Detects current checked state before clicking to avoid double-toggle

---

### Phase 7 — Human-in-the-Loop (HITL) Full Implementation (May 21–26, 2026)

**Commits:** `f82ef4f` (hItl), `c2bec50` (hitl improvements)

This was the second major architectural feature after perception.

**Problem:** When execution fails and selector recovery cannot find the element automatically, the test completely fails. There is no way for a human to intervene mid-run, perform the action manually in the browser, and let the run continue from that point.

**Solution — HITL recovery flow:**

1. When a step hits a hard failure (after all automatic retries), instead of marking the run `failed`, it transitions to `waiting_for_input` with `user_input_kind = "recovery_confirm"`.
2. The UI shows a "Take over" panel — the user can see the live browser via the viewer session.
3. A JS recorder is injected into the browser: `start_interaction_recording(run_id)`.
4. The user performs the action manually in the live browser.
5. Every interaction (click, type, select) is captured and stored in a JS buffer.
6. The UI shows a "I'm done — Resume" button.
7. `POST /api/runs/{run_id}/steps/{step_id}/recovery-confirm` is called.
8. The backend reads `get_recorded_interactions(run_id)`, normalizes them into steps, marks the step `human_recovered`, resumes the run.
9. The interaction sequence is saved to `RecoveryRecipeStore` so the next run attempts automatic replay.

**New files:**
- `hitl_manager.py` — owns browser-side JS recorder lifecycle (start/stop/read)
- `hitl_replayer.py` — replays recorded interaction sequences as automated actions

**New API endpoints:**
- `POST /api/runs/{run_id}/steps/{step_id}/recovery-confirm` — confirm human recovery, resume run

**Effect:** Runs that previously would fail completely can now be unblocked by a human without restarting. Over time, the recipe store accumulates these recoveries and they become automatic.

---

### Phase 8 — Replay Logic (June 4, 2026)

**Commit:** `ce1cb02` — "replay logic"

**What was added:**
- `HitlReplayer` class fully implemented — takes a recorded interaction list and converts each event to an equivalent `StepRuntimeState` for automated replay
- `recovery/snapshot.py` — `diff_recovery_snapshots()` compares page state before and after HITL to identify what changed
- `build_failure_context()` and `build_failure_context_from_summary()` — encode the failure context (URL path, visible error text, missing element info) for recipe matching
- `promote_interactions_to_actions()` — converts raw JS interaction events into `RecoveryAction` objects for the recipe store
- `RecoveryRecipe` matching now uses failure context similarity, not just element key, for more precise replay targeting

**Effect:** Automatic replay of previously human-solved steps is now reliable. The system remembers not just "what worked" but "under what page conditions it worked."

---

## 5. Feature Log

### Feature Summary Table

| Feature | Added | Commit | Effect |
|---|---|---|---|
| Core execution engine | Mar 4 | `7f5d6db` | Foundation for all automation |
| Drag-and-drop | Mar 5 | `0eff958` | Support for drag-based UI builders |
| HTML run reports | Mar 17 | `79862f5` | Non-technical stakeholder visibility |
| Selector memory | Mar 23 | `3d8e1fc` | Self-healing selector failures |
| Recovery recipes | Mar 23 | `3d8e1fc` | Automated replay of past fixes |
| Suite executor | Mar 23 | `3d8e1fc` | Batch test case execution |
| Test case CRUD | Mar 23 | `3d8e1fc` | Reusable named test cases |
| JWT auth + cookies | Mar 27 | `daee824` | Multi-user, secure access |
| CSRF protection | Mar 27 | `daee824` | API security hardening |
| Docker deployment | Apr 11 | `8b43ca8` | Cloud deployable |
| CI pipelines | Apr 11 | `8b43ca8` | Automated test/build on PR |
| Browser viewer (VNC) | Apr 13–15 | `27472f6` | Real-time browser visibility |
| Perception engine | Apr 8 | `895e8d9` | Reliable element identification |
| Plan validation | Apr 8 | `895e8d9` | Catch bad LLM plans before execution |
| Step source map | Apr 8 | `895e8d9` | Traceability from instruction to action |
| Selector recovery (HITL-lite) | Apr 21 | `9cee2e1` | Human provides selector, run resumes |
| Intent builder | Apr 29 | `a00c64d` | Structured semantic step intent |
| Selector utils | Apr 29 | `a00c64d` | Reusable selector helper library |
| Semantic routing | May 13 | `4b4af9a` | Direct ARIA-based element match |
| SemanticTarget v2 | May 15 | `b842702` | Cleaner semantic contract schema |
| Checkbox toggling | May 17 | `23113e5` | State-aware checkbox interaction |
| HITL full recovery | May 21 | `f82ef4f` | Human takes over, run resumes |
| HITL replay | May 26 | `c2bec50` | Recorded interactions become automation |
| Recovery snapshots | Jun 4 | `ce1cb02` | Context-aware recipe matching |
| Step import (CSV/XLSX) | (backlog) | `3d8e1fc` | Bulk test case creation from spreadsheet |
| Template engine | (backlog) | `3d8e1fc` | `{{variable}}` placeholders in steps |
| Page health checks | (feature branch) | `page_health.py` | Detect broken pages before execution |

---

## 6. All Modules, Classes, and Key Functions

### `backend/app/main.py`

The FastAPI application factory and all HTTP endpoints. Also contains step plan utilities.

| Symbol | Type | Description |
|---|---|---|
| `build_app()` | function | Factory — creates the FastAPI app, wires all dependencies |
| `_sanitize_plan_steps()` | function | Canonicalize and validate steps from LLM output; drop invalid/duplicate steps |
| `_expand_drag_steps()` | function | Expand drag steps into click → drag → wait sub-sequence |
| `_build_step_source_map()` | function | Map each action index back to its source instruction line |
| `_validate_generated_plan()` | function | Hard/soft validation of LLM-generated plans |
| `_split_prompt_into_action_lines()` | function | Split free-form prompt into individual action lines |
| `_action_line_to_text()` | function | Convert a natural-language action line to canonical human-readable text |
| `_step_to_text()` | function | Convert a step JSON dict to a short human-readable description |
| `_selector_to_label()` | function | Extract readable label from a raw CSS/Playwright selector |
| `build_admin_auth_dependency()` | function | Returns a FastAPI dependency for admin token auth |
| `_resolve_start_url()` | function | Find start URL from explicit field or first navigate step |

---

### `backend/app/runtime/executor.py`

The core step-by-step execution engine. ~2,300 lines.

| Symbol | Type | Description |
|---|---|---|
| `AgentExecutor` | class | Orchestrates the full execution of a run |
| `AgentExecutor.execute()` | method | Main entrypoint — processes all steps of a run |
| `AgentExecutor._execute_step()` | method | Executes a single step; handles retries, perception, fallback |
| `AgentExecutor._try_perception_match()` | method | Attempt DOM-first element identification before any action |
| `AgentExecutor._try_recipe_replay()` | method | Attempt automatic replay of a previously saved recovery recipe |
| `AgentExecutor._try_selector_memory()` | method | Look up historically successful selectors from memory store |
| `AgentExecutor._handle_step_failure()` | method | Classify failure, decide: retry / HITL / fail |
| `AgentExecutor.apply_manual_selector_hint()` | method | Accept a human-provided selector for a paused step |
| `AgentExecutor.apply_human_recovery_confirmation()` | method | Mark step human_recovered, resume run after HITL session |
| `AgentExecutor._extract_element_key()` | static | Derive a stable domain+step key for selector memory lookup |
| `AgentExecutor._extract_domain()` | static | Extract bare domain from a URL |
| `AgentExecutor._build_failure_context_from_summary()` | static | Build `FailureContext` from a page snapshot summary |
| `CandidateConfidence` | dataclass | Confidence level + score gap for a matched element candidate |
| `GroundedCandidate` | dataclass | A scored + ordered element candidate with selectors |

---

### `backend/app/runtime/perception.py`

Perceive-first element identification from live DOM snapshots.

| Symbol | Type | Description |
|---|---|---|
| `IndexedElement` | dataclass | A single interactive element from a live page snapshot |
| `ElementIndex` | class | Scored index of all interactive elements on the current page |
| `PerceptionMatch` | dataclass | Matched element with confidence level and derived selector |
| `build_element_index()` | function | Parse an `inspect_page()` snapshot into an `ElementIndex` |
| `find_best_match()` | function | Score all elements against intent text + step type; return best match |
| `find_best_match_for_target()` | function | Score against full `SemanticTarget` (ARIA role, accessible name, scope) |
| `find_by_signatures()` | function | Re-identify an element using stored semantic fingerprints |
| `derive_element_selectors()` | function | Build an ordered list of selectors (most → least stable) for an element |

---

### `backend/app/runtime/intent_builder.py`

Builds a structured semantic intent from a step definition.

| Symbol | Type | Description |
|---|---|---|
| `StepIntent` | dataclass | Structured intent: element type, target text, accessible name, ordinal, scope hint |
| `build_step_intent()` | function | Build `StepIntent` from a step dict + `SemanticTarget` |
| `editable_selector_field()` | function | Return the selector field name for a given step type |
| `intent_element_type()` | function | Infer the element type (button, textbox, combobox…) from step intent |
| `intent_ordinal()` | function | Extract ordinal hint (1st, 2nd…) from intent text |
| `intent_scope_hint()` | function | Extract container/scope hint from intent |
| `intent_target_text()` | function | Extract the primary target text from intent |
| `infer_tag_from_selector()` | function | Guess the HTML tag from a CSS selector |
| `infer_type_from_selector()` | function | Guess input[type] from selector pattern (e.g., "password") |
| `selector_seed_from_target()` | function | Derive a fallback CSS selector seed from a semantic target |
| `serialize_step_intent()` | function | Serialize a `StepIntent` to a debug dict |
| `step_context_hint()` | function | Extract context hint from a step for logging |
| `step_has_semantic_contract()` | function | True if step has enough semantic info to bypass brittle selector lookup |
| `step_text_hint()` | function | Short human-readable hint for a step (used in logs) |

---

### `backend/app/runtime/hitl_manager.py`

Owns the browser-side JS interaction recorder lifecycle during HITL sessions.

| Symbol | Type | Description |
|---|---|---|
| `HitlManager` | class | Manages recording lifecycle |
| `HitlManager.start_recording()` | method | Inject JS recorder into the live page |
| `HitlManager.stop_recording()` | method | Disable and clear the browser-side recorder |
| `HitlManager.read_interactions()` | method | Read and atomically clear the interaction buffer |
| `HitlManager.build_confirmation_prompt()` | method | Build human-readable summary of recorded interactions |

---

### `backend/app/runtime/hitl_replayer.py`

Converts recorded human interactions into automated replay steps.

| Symbol | Type | Description |
|---|---|---|
| `HitlReplayer` | class | Replays recorded interaction sequences |
| `HitlReplayer.replay()` | method | Execute recorded interactions as automated steps |
| `HitlReplayer.normalize_interactions()` | method | Convert raw JS events to clean interaction sequence |

---

### `backend/app/runtime/recovery/snapshot.py`

Snapshot utilities for recovery context and recipe matching.

| Symbol | Type | Description |
|---|---|---|
| `build_failure_context()` | function | Build a `FailureContext` from a live page snapshot |
| `build_failure_context_from_summary()` | function | Build `FailureContext` from a pre-computed summary string |
| `derive_success_signals()` | function | Extract signals from a successful post-action snapshot |
| `diff_recovery_snapshots()` | function | Compare before/after snapshots to identify page changes |
| `extract_domain()` | function | Extract bare domain from a URL |
| `extract_element_key()` | function | Derive a stable step+selector key for memory/recipe lookup |
| `promote_interactions_to_actions()` | function | Convert JS interaction events to `RecoveryAction` objects |
| `snapshot_item_summary()` | function | Generate a text summary of a snapshot item for logging |

---

### `backend/app/runtime/selector_memory.py`

SQLite-backed store for learning which selectors succeeded per domain/step.

| Symbol | Type | Description |
|---|---|---|
| `SelectorMemoryStore` | protocol | Interface for selector memory stores |
| `remember_success()` | method | Record a successfully used selector |
| `get_candidates()` | method | Retrieve historically successful selectors for a domain/step |
| `remember_signature()` | method | Store an element's semantic fingerprint |
| `get_signatures()` | method | Retrieve stored element fingerprints for re-identification |
| `build_selector_memory_store()` | function | Factory — creates the SQLite-backed store from settings |

---

### `backend/app/runtime/recovery_recipe_store.py`

Stores and retrieves full action recipes for automated failure recovery.

| Symbol | Type | Description |
|---|---|---|
| `RecoveryRecipeStore` | class | SQLite-backed recipe persistence |
| `save_recipe()` | method | Persist a `RecoveryRecipe` |
| `find_recipes()` | method | Find matching recipes by domain + step type + element key |
| `build_recovery_recipe_store()` | function | Factory from settings |

---

### `backend/app/runtime/store.py`

In-memory + SQLite run state store.

| Symbol | Type | Description |
|---|---|---|
| `RunStore` | class | CRUD + persistence for `RunState` objects |
| `RunStore.create()` | method | Create a new `RunState` from a `RunCreateRequest` |
| `RunStore.get()` | method | Retrieve a run by ID |
| `RunStore.list()` | method | List all runs (optionally filtered) |
| `RunStore.persist()` | method | Write run state to SQLite |
| `build_run_store()` | function | Factory from settings |

---

### `backend/app/runtime/suite_executor.py`

Batch execution of multiple test cases as a suite.

| Symbol | Type | Description |
|---|---|---|
| `SuiteExecutor` | class | Orchestrates suite-level test execution |
| `SuiteExecutor.execute()` | method | Run all test cases in a suite sequentially |
| `SuiteExecutor.cancel()` | method | Cancel a running suite |

---

### `backend/app/runtime/test_case_store.py`

CRUD persistence for named, reusable test cases and folders.

| Symbol | Type | Description |
|---|---|---|
| `TestCaseStore` | class | In-memory + SQLite store for test cases and folders |
| `create()` | method | Create a new test case |
| `create_folder()` | method | Create a test folder |
| `get()` / `list()` / `delete()` | methods | CRUD operations |
| `persist()` | method | Write to SQLite |

---

### `backend/app/runtime/plan_normalizer.py`

Normalize raw LLM plan steps into the canonical step schema.

| Symbol | Type | Description |
|---|---|---|
| `normalize_plan_steps()` | function | Convert raw LLM output steps to canonical step dicts |
| `build_recovery_steps()` | function | Build recovery steps from a `RecoveryRecipe` |

---

### `backend/app/runtime/instruction_parser.py`

Parse structured natural-language task descriptions into steps (bypasses LLM).

| Symbol | Type | Description |
|---|---|---|
| `parse_structured_task_steps()` | function | Rule-based parser: converts structured task text to step list |

---

### `backend/app/runtime/template_engine.py`

`{{variable}}` placeholder substitution in step fields.

| Symbol | Type | Description |
|---|---|---|
| `TemplateEngine` | class | Renders template placeholders in step definitions |
| `TemplateEngine.render()` | method | Substitute `{{key}}` with values from test_data + selector_profile |

---

### `backend/app/runtime/page_health.py`

Detect broken or blocked page states before executing a step.

| Symbol | Type | Description |
|---|---|---|
| `check_page_health()` | function | Returns a health report: error pages, auth walls, loading states |
| `looks_like_popup_blocker()` | function | Detect popup/overlay blocking the main content |

---

### `backend/app/runtime/report_builder.py`

Generate HTML run reports.

| Symbol | Type | Description |
|---|---|---|
| `build_html_report()` | function | Build a full HTML report for a completed run |
| `build_summary()` | function | Build a short text summary (used in `summary.txt` artifact) |

---

### `backend/app/runtime/viewer_session.py`

Per-run isolated browser viewer sessions (VNC/screenshot streaming).

| Symbol | Type | Description |
|---|---|---|
| `ViewerSessionManager` | class | Manages viewer sessions keyed by run_id |
| `prepare_run()` | method | Allocate or retrieve a viewer session for a run |
| `aclose()` | method | Clean up all sessions on shutdown |

---

### `backend/app/mcp/browser_client.py`

Playwright-based browser automation client (~1,400 lines).

| Symbol | Type | Description |
|---|---|---|
| `BrowserMCPClient` | class | All browser interactions via Playwright |
| `navigate()` | method | Navigate to a URL |
| `click()` | method | Click an element by selector |
| `type_text()` | method | Type text into an input field |
| `select_option()` | method | Select a dropdown option by value or text |
| `scroll()` | method | Scroll page or a container |
| `drag_and_drop()` | method | Drag element to target (3-strategy: native → mouse events → JS dispatch) |
| `handle_popup()` | method | Accept/dismiss/close a dialog |
| `verify_text()` | method | Assert text is present on page |
| `verify_image()` | method | Compare screenshot to baseline |
| `inspect_page()` | method | Return a full DOM/ARIA snapshot of the current page |
| `screenshot()` | method | Capture a screenshot of the current page |
| `start_interaction_recording()` | method | Inject JS recorder for HITL sessions |
| `stop_interaction_recording()` | method | Stop and clear the JS recorder |
| `get_recorded_interactions()` | method | Read the JS interaction buffer |
| `build_browser_client()` | function | Factory from settings |

---

### `backend/app/mcp/filesystem_client.py`

File system MCP client for artifact access.

| Symbol | Type | Description |
|---|---|---|
| `FileSystemClient` | class | Controlled file read/write for artifact logging |
| `read_file()` | method | Read an artifact file |
| `write_file()` | method | Write an artifact file |
| `list_directory()` | method | List artifact directory contents |
| `build_filesystem_client()` | function | Factory from settings |

---

### `backend/app/brain/`

HTTP client that calls the Brain service.

| Symbol | Type | Description |
|---|---|---|
| `BrainClient` | protocol (`base.py`) | Interface all brain clients implement |
| `HttpBrainClient` | class (`http_client.py`) | HTTP client for the brain service |
| `HttpBrainClient.plan_task()` | method | Call `/plan` — convert task text to JSON step list |
| `HttpBrainClient.human_steps()` | method | Call `/human_steps` — expand prompt into plain-English action lines |
| `HttpBrainClient.healthcheck()` | method | Check brain service health + LLM mode |

---

### `backend/app/auth/`

JWT-based authentication.

| Symbol | File | Description |
|---|---|---|
| `AuthService` | `service.py` | Register, login, refresh, logout logic |
| `create_access_token()` | `security.py` | Issue a signed JWT access token |
| `verify_token()` | `security.py` | Validate and decode a JWT |
| `hash_password()` | `security.py` | Bcrypt hash |
| `verify_password()` | `security.py` | Bcrypt compare |
| `require_authenticated_user` | `dependencies.py` | FastAPI dependency: enforce auth |
| `build_api_auth_dependency()` | `dependencies.py` | Factory that respects `AUTH_ENABLED` setting |
| `CSRFMiddleware` | `csrf.py` | Double-submit CSRF cookie validation |
| `RateLimiter` | `rate_limiter.py` | Sliding-window rate limiter for auth endpoints |

---

### `brain/app/`

Standalone LLM service.

| Symbol | File | Description |
|---|---|---|
| `LLMProvider` | `llm/base.py` | Abstract base for all LLM providers |
| `OpenAIProvider` | `llm/openai_provider.py` | OpenAI (GPT-4o, etc.) via `openai` SDK |
| `AnthropicProvider` | `llm/anthropic_provider.py` | Claude via Anthropic SDK |
| `LocalVLLMProvider` | `llm/local_vllm.py` | Local vLLM endpoint |
| `build_llm_provider()` | `llm/factory.py` | Factory: reads `LLM_MODE`, returns correct provider |
| Brain `/plan` endpoint | `main.py` | Accepts task text, returns `{steps: [...]}` JSON |
| Brain `/human_steps` endpoint | `main.py` | Accepts task text, returns `[step1, step2, ...]` plain-English lines |
| Brain `/health` endpoint | `main.py` | Returns current LLM mode and model name |

---

### `frontend/src/`

Next.js UI.

| File | Description |
|---|---|
| `app/page.tsx` | Home page — task builder, run list, run launch |
| `app/layout.tsx` | Root layout with nav/header |
| `app/test-cases/[testCaseId]/page.tsx` | Test case detail and editor |
| `components/AutocompleteInput.tsx` | Step description autocomplete input |
| `lib/api-auth.ts` | Auth helper — attach cookies/tokens to fetch calls |
| `lib/autocompleteStore.ts` | Autocomplete suggestion state |
| `lib/useAutocomplete.ts` | Autocomplete hook |
| `lib/wordDictionary.ts` | Built-in action vocabulary for autocomplete |

---

## 7. API Reference

### Public Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | None | Service health + LLM mode |
| `GET` | `/api/config` | None | Runtime config (LLM mode, browser mode, auth flags) |
| `POST` | `/api/runs` | User | Create and start a new run |
| `GET` | `/api/runs` | User | List runs |
| `GET` | `/api/runs/{run_id}` | User | Get a run's current state |
| `POST` | `/api/runs/{run_id}/cancel` | User | Cancel a running run |
| `POST` | `/api/runs/{run_id}/steps/{step_id}/selector` | User | Provide a manual selector for a paused step |
| `POST` | `/api/runs/{run_id}/steps/{step_id}/recovery-confirm` | User | Confirm human recovery, resume run |
| `POST` | `/api/test-cases` | User | Create a test case |
| `GET` | `/api/test-cases` | User | List test cases |
| `GET` | `/api/test-cases/{id}` | User | Get a test case |
| `PUT` | `/api/test-cases/{id}` | User | Update a test case |
| `DELETE` | `/api/test-cases/{id}` | User | Delete a test case |
| `POST` | `/api/test-cases/{id}/run` | User | Execute a test case as a new run |
| `POST` | `/api/test-cases/import` | Admin | Import test steps from CSV/XLSX |
| `POST` | `/api/test-folders` | User | Create a folder |
| `GET` | `/api/test-folders` | User | List folders |
| `DELETE` | `/api/test-folders/{id}` | User | Delete folder + cascade children |
| `POST` | `/api/plan` | Admin | Generate a plan from a task description |
| `POST` | `/api/prompt-to-steps` | Admin | Convert prompt to human-readable step list |
| `POST` | `/api/suite-runs` | User | Create and start a suite run |
| `GET` | `/api/suite-runs/{id}` | User | Get suite run state |
| `GET` | `/api/artifacts/{run_id}/{filename}` | User | Serve run artifact (log, screenshot, report) |

### Auth Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/auth/register` | Register a new user |
| `POST` | `/auth/login` | Login — returns JWT + sets cookie |
| `POST` | `/auth/refresh` | Refresh access token |
| `POST` | `/auth/logout` | Logout — clears cookie |
| `GET` | `/auth/me` | Get current user info |

---

## 8. Data Models and Schemas

### Core Run Models (`backend/app/schemas.py`)

| Model | Description |
|---|---|
| `RunState` | Full state of a run: id, name, status, steps, start/end time, artifacts |
| `RunStatus` | Enum: `pending`, `running`, `waiting_for_input`, `completed`, `failed`, `cancelled` |
| `StepRuntimeState` | State of a single step: id, type, input, status, start/end, message, artifact_path |
| `StepStatus` | Enum: `pending`, `running`, `waiting_for_input`, `completed`, `failed`, `skipped`, `cancelled`, `human_recovered` |
| `SemanticTarget` | Semantic element contract: `semantic_name`, `expected_role`, `accessible_name`, `scope` (+ legacy fields) |
| `RunCreateRequest` | Request body for `POST /api/runs` |
| `TestCaseState` | Stored test case: id, name, description, steps, test_data, selector_profile |
| `SuiteRunState` | Suite run state: suite_id, test_case runs, overall status |
| `RecoveryContext` | Context captured at failure: current URL, failure snapshot, visible elements |
| `RecoveryRecipe` | Stored recovery recipe: domain, step_type, element_key, failure_context, actions |
| `RecoveryAction` | A single action in a recipe: action_type (fill_field, click_control, select_option), selectors, value |
| `FailureContext` | Failure conditions for recipe matching: URL path, error text |
| `SuccessSignals` | Signals that indicate success after replay |
| `InteractionEvent` | A raw JS-recorded user interaction (click, type, select) |

### Step Types

| Step | Required Fields | Optional Fields |
|---|---|---|
| `navigate` | `url` | — |
| `click` | `selector` or `target` | `text_hint` |
| `type` | `selector` or `target`, `text` | `clear_first` (default: true) |
| `select` | `selector` or `target`, `value` | — |
| `drag` | `source_selector`, `target_selector` | — |
| `scroll` | `direction` | `target` (page or selector), `amount` |
| `wait` | `until` | `ms`, `selector`, `load_state` |
| `handle_popup` | `policy` | `selector` |
| `verify_text` | `selector` or `target`, `value` | `match` (exact/contains/regex) |
| `verify_image` | — | `selector`, `baseline_path`, `threshold` |

---

## 9. Configuration Reference

### Backend (`backend/.env`)

| Variable | Default | Description |
|---|---|---|
| `BRAIN_BASE_URL` | `http://localhost:8090` | Brain service URL |
| `BRAIN_API_KEY` | _(none)_ | Optional API key for brain service |
| `BROWSER_MODE` | `mock` | `mock` or `playwright` |
| `BROWSER_VIEWER_ENABLED` | `false` | Enable VNC-based run viewer |
| `PLAYWRIGHT_HEADLESS` | `true` | Run Chromium in headless mode |
| `FILESYSTEM_MODE` | `local` | `local` or `mcp` |
| `RUN_STORE_BACKEND` | `sqlite` | `memory` or `sqlite` |
| `ARTIFACT_ROOT` | `./artifacts` | Path for run artifacts |
| `MAX_STEPS_PER_RUN` | `50` | Hard cap on steps per run |
| `AUTH_ENABLED` | `false` | Enable JWT auth |
| `ADMIN_API_TOKEN` | _(none)_ | Static token for admin endpoints |
| `CORS_ORIGINS` | `*` | Allowed CORS origins |
| `LOG_LEVEL` | `INFO` | Logging level |
| `RECOVERY_RECIPE_ENABLED` | `true` | Enable recipe store for auto-recovery |

### Brain (`brain/.env`)

| Variable | Default | Description |
|---|---|---|
| `LLM_MODE` | `cloud` | `local` (vLLM), `cloud` (OpenAI), `anthropic` |
| `OPENAI_API_KEY` | _(required for cloud)_ | OpenAI API key |
| `OPENAI_MODEL` | `gpt-4o` | Model name for OpenAI |
| `ANTHROPIC_API_KEY` | _(required for anthropic)_ | Anthropic API key |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Model name for Anthropic |
| `VLLM_BASE_URL` | `http://localhost:8000` | vLLM endpoint |
| `MAX_TOKENS` | `4096` | Max tokens per LLM response |

---

## 10. Blockers and Challenges

### Challenge 1 — Brittle LLM-Generated Selectors (Feb–Apr 2026)

**Problem:** The LLM generates CSS selectors at plan time. By execution time, the DOM may have changed slightly (dynamic IDs, re-ordered elements), causing hard failures.

**Attempts:**
1. Added `SelectorMemoryStore` — remember past successes. Helped but didn't cover new pages.
2. Added structured selector profiles in test cases — user could override selectors. Helped but required manual work.

**Resolution (April 8):** The perception engine rewrote this entirely. Instead of using LLM selectors directly, the executor now observes the live DOM, scores all elements against the step's semantic intent, and derives a fresh selector from the matching element. This made the system fundamentally more robust.

**Residual:** Perception still fails on pages with no ARIA roles, poor labeling, or heavily dynamic rendering. These cases fall back to the original brittle path.

---

### Challenge 2 — Drag-and-Drop Reliability (March 2026)

**Problem:** Playwright's native `drag_and_drop()` works on some apps but fails silently on others that rely on custom drag events (React DnD, SortableJS, etc.).

**Attempts:**
1. Native Playwright API — works ~60% of the time.
2. Manual mouse event sequence (`mousedown → mousemove → mouseup`) — works on more apps but timing-sensitive.
3. JavaScript `DataTransfer` injection — works on apps using the HTML5 Drag-and-Drop API.

**Resolution:** Three-strategy cascade in `browser_client.py`. Strategies are tried in order; first success wins. Post-drag wait step added to let the DOM settle.

**Residual:** Some apps (especially those using virtual lists or canvas-based drag) still fail. The `data/drag_debug.jsonl` file exists from debugging sessions.

---

### Challenge 3 — Plan Quality (March–May 2026)

**Problem:** LLM-generated plans have issues:
- Steps are merged (two actions become one step, one gets dropped)
- Extra steps added that weren't in the prompt
- Generic selectors like `body`, `h1` that always match but verify nothing useful
- Steps reference placeholder text ("Example Domain") when given a non-example site

**Attempts:**
1. Added planner constraints in the planning prompt (explicit selector rules, step type list)
2. Added `human_steps` pre-expansion — expand prompt into plain-English lines before planning
3. Added `_sanitize_plan_steps()` — post-process: drop generics, fix obvious issues
4. Added `_validate_generated_plan()` — hard block on empty plans; soft warn on missing steps

**Residual:** Plan quality is LLM-dependent. Prompt engineering needs ongoing work as new app types are tested.

---

### Challenge 4 — HITL Architecture Complexity (May 2026)

**Problem:** Implementing true mid-run human intervention required threading multiple systems: run state machine, browser session ownership, JS event capture, interaction replay.

**Specific sub-problems:**
- The executor holds a reference to the browser page — how does HITL "hand off" control without the executor closing the page?
- How to capture user actions in the live browser (not in Playwright, in the user's view)?
- How to normalize diverse interaction events into replayable steps?

**Resolution:**
- Executor transitions to `waiting_for_input` and pauses (polling loop + asyncio event).
- Viewer session keeps the page alive; user sees it via VNC.
- JS recorder injected into the page via `evaluate()` — captures all clicks, types, selects into a buffer.
- `HitlManager.read_interactions()` polls and clears the buffer.
- On confirmation, `HitlReplayer.normalize_interactions()` converts raw events to step schema.

**Residual:** JS recorder has limited visibility into shadow DOM and iframes. Complex interactions (drag, file upload) are not captured.

---

### Challenge 5 — Windows Development Environment (Ongoing)

**Problem:** Primary development is on Windows 10. Playwright, Xvfb, and VNC are Linux-native. Several tools behave differently on Windows (asyncio loop policy, path separators, file locking).

**Workarounds:**
- `asyncio.WindowsProactorEventLoopPolicy` set on startup (required for Playwright subprocess support on Windows)
- `dev.bat` script for Windows local startup
- Docker used for staging/production to run Linux-native components
- `start_with_display.sh` for Linux VNC session startup

**Residual:** Full VNC viewer only works in Docker on Linux. Development on Windows uses headless Playwright without viewer.

---

### Challenge 6 — Checkbox and Toggle Interactions (May 2026)

**Problem:** Checkboxes and toggle switches have two states. A naïve `click()` call may toggle from checked → unchecked when the intent was to check it, or vice versa.

**Resolution:** Step execution now reads the current `checked` state before clicking. If the desired end state matches the current state, the click is skipped.

---

## 11. Current State (June 2026)

### What Works

- Full plan-and-execute pipeline: prompt → LLM plan → Playwright execution
- Perception-first element identification (reliable on well-labeled apps)
- Semantic routing for ARIA-rich pages (login forms, standard controls)
- Selector memory + recipe store: self-healing for known-broken selectors
- HITL full recovery: human takes over, run resumes, interaction saved for replay
- Test case CRUD + folder organization
- Suite execution (batch run)
- HTML run reports with per-step artifacts
- JWT auth + cookie sessions
- Docker compose deployment with Nginx
- CI pipelines (backend + frontend)
- Step import from CSV/XLSX

### Active Branch Work (`feature/human-interventin`)

Modified files pending merge:
- `backend/app/mcp/browser_client.py` — drag strategy improvements, interaction recorder
- `backend/app/runtime/executor.py` — HITL integration, recipe replay
- `backend/app/runtime/perception.py` — ARIA scope expansion, signature matching
- `backend/tests/test_executor_selector_fallback.py` — HITL tests

New files not yet committed to main:
- `backend/app/runtime/intent_builder.py`
- `backend/app/runtime/page_health.py`
- `backend/app/runtime/recovery/snapshot.py`
- `backend/app/runtime/report_builder.py`
- `backend/app/runtime/selectors/utils.py`
- `backend/app/runtime/template_engine.py`

---

## 12. Known Gaps and Next Steps

| Gap | Priority | Notes |
|---|---|---|
| Shadow DOM / iframe perception | High | Perception engine currently skips shadow roots |
| HITL capture of drag/file upload | Medium | JS recorder only handles click/type/select |
| Agentic fallback (screenshot diagnosis) | Medium | When DOM is opaque, use screenshot + LLM to identify element visually |
| Full VNC viewer on Windows dev | Low | Docker workaround exists |
| Plan quality for complex multi-page flows | High | LLM often drops steps on 10+ step tasks |
| `verify_image` baseline management | Low | No UI for managing baseline screenshots |
| Parallel test suite execution | Medium | Currently sequential only |
| Selectors for Canvas/WebGL apps | Low | Not feasible without screenshot-based grounding |
| Test result trend reporting | Medium | No historical pass/fail trend view yet |

---

*This document was generated on 2026-06-06 from git history, source code, and all existing docs in the repository.*
