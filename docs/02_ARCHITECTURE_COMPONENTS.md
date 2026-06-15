# Document 2: Architecture & Components — The Present

> **Purpose:** What exists today, which component owns which responsibility, and which files/classes implement it.  
> **As of:** June 2026

---

## System Overview

Tekno-Phantom-Agent is a three-service system connected by HTTP. Each service has a single responsibility and can be replaced independently.

```
┌──────────────────────────────────────────────────────────────────┐
│                        Nginx (port 80)                           │
│  / → Frontend   /api/* → Backend   /auth/* → Backend            │
└──────┬──────────────────────┬───────────────────────────────────┘
       │                      │
       ▼                      ▼
┌─────────────┐      ┌─────────────────────────────────────────┐
│  Frontend   │      │             Backend                     │
│  Next.js 16 │ ◄──► │  FastAPI · Python 3.11 · port 8080      │
│  React 19   │      │                                         │
│  TypeScript │      │  ┌──────────┐  ┌───────────────────┐   │
└─────────────┘      │  │  Runtime │  │  Auth / Projects  │   │
                     │  │  Engine  │  │  Routes           │   │
                     │  └────┬─────┘  └───────────────────┘   │
                     │       │                                  │
                     │  ┌────▼─────┐  ┌──────────────────┐    │
                     │  │Playwright│  │  SQLite (×3)      │    │
                     │  │Browser   │  │  auth.db          │    │
                     │  │Chromium  │  │  run_store        │    │
                     │  └──────────┘  │  selector_memory  │    │
                     └────────────────┴──────────────────-┘
                                │
                          HTTP /plan
                                ▼
                     ┌──────────────────────┐
                     │       Brain          │
                     │  FastAPI · port 8090 │
                     │  OpenAI / Anthropic  │
                     │  / local vLLM        │
                     └──────────────────────┘
```

---

## Component 1: Frontend

**Purpose:** User interface for authoring test cases, launching runs, and monitoring results in real time. Also provides the HITL recovery panel.

**Technology:** Next.js 16 (App Router), React 19, TypeScript, CSS Modules.

**Files:**

```
frontend/src/
├── app/
│   ├── page.tsx                          ← Home: run list + task builder
│   ├── layout.tsx                        ← Root layout with nav
│   ├── globals.css
│   └── test-cases/
│       └── [testCaseId]/
│           └── page.tsx                  ← Test case detail + editor
├── components/
│   └── AutocompleteInput.tsx             ← Step description autocomplete
└── lib/
    ├── api-auth.ts                       ← Attach JWT/cookies to all API calls
    ├── autocompleteStore.ts              ← Autocomplete suggestion state (Zustand-style)
    ├── useAutocomplete.ts                ← Autocomplete hook
    └── wordDictionary.ts                 ← Built-in step action vocabulary
```

**Inputs:** User interactions (click, type, submit). Polls `GET /api/runs/{id}` for live status.

**Outputs:** `POST /api/runs`, `POST /api/test-cases`, `POST /api/runs/{id}/steps/{id}/recovery-confirm`, and all other API calls.

**Key behavior:**
- Run status polling updates the step list in real-time (no WebSocket — simple polling)
- HITL panel appears when a run's status is `waiting_for_input` with `user_input_kind = "recovery_confirm"`
- Viewer iframe shown when `run.viewer_url` is set (VNC session live)

---

## Component 2: Backend — API Layer

**Purpose:** Validate all inbound requests, own the HTTP contract, wire dependencies, and delegate to the Runtime Engine. Also owns auth.

**Technology:** FastAPI, Pydantic, SQLAlchemy, Python 3.11.

**Entry point:** `backend/app/main.py` — `build_app()` factory.

**Files:**

```
backend/app/
├── main.py              ← FastAPI app factory + all route handlers + plan utilities
├── config.py            ← Settings (Pydantic BaseSettings from .env)
├── database.py          ← SQLAlchemy engine init for auth.db
├── schemas.py           ← All Pydantic request/response models
├── logging_config.py    ← Structured logging setup
├── auth/
│   ├── service.py       ← Register, login, refresh, logout logic
│   ├── security.py      ← JWT sign/verify, bcrypt hash/compare
│   ├── dependencies.py  ← FastAPI auth dependencies (require_authenticated_user)
│   ├── csrf.py          ← Double-submit CSRF cookie validation
│   ├── rate_limiter.py  ← Sliding-window IP + identity rate limiting
│   └── schemas.py       ← Auth-specific request/response models
├── models/
│   ├── user.py          ← SQLAlchemy User ORM model
│   ├── project.py       ← SQLAlchemy Project ORM model
│   └── refresh_token.py ← SQLAlchemy RefreshToken ORM model
├── routes/
│   ├── auth.py          ← /auth/* route handlers
│   └── projects.py      ← /api/projects/* route handlers
└── brain/
    ├── base.py          ← BrainClient protocol (interface)
    └── http_client.py   ← HttpBrainClient — calls the Brain service via HTTP
```

**Inputs:** HTTP requests from Frontend or direct API clients.

**Outputs:**
- Creates `RunState` objects and dispatches background execution tasks
- Calls `Brain` service for plan generation and step expansion
- Writes artifacts to disk (`artifacts/<run_id>/`)

**Key classes:**

| Class / Function | File | Responsibility |
|---|---|---|
| `build_app()` | `main.py` | Wire all dependencies, register routes, return FastAPI app |
| `_sanitize_plan_steps()` | `main.py` | Validate and canonicalize LLM-generated step lists |
| `_expand_drag_steps()` | `main.py` | Expand drag into click→drag→wait sub-sequence |
| `_validate_generated_plan()` | `main.py` | Hard reject empty plans; soft warn on suspicious plans |
| `HttpBrainClient` | `brain/http_client.py` | All outbound calls to Brain service |
| `AuthService` | `auth/service.py` | User registration, login, token refresh, logout |
| `Settings` | `config.py` | All configuration loaded from `.env` at startup |

---

## Component 3: Backend — Runtime Engine

**Purpose:** Execute test cases step by step inside a real browser. This is the core of the product.

**Files:**

```
backend/app/runtime/
├── executor.py              ← AgentExecutor — main execution engine (~2,300 lines)
├── perception.py            ← DOM snapshot → element index → best match
├── intent_builder.py        ← Structured StepIntent from step definition
├── plan_normalizer.py       ← Normalize raw LLM step output to canonical schema
├── instruction_parser.py    ← Rule-based parser: structured text → steps (bypasses LLM)
├── template_engine.py       ← {{variable}} substitution in step fields
├── page_health.py           ← Detect broken/blocked page states
├── hitl_manager.py          ← JS recorder lifecycle (start/stop/read)
├── hitl_replayer.py         ← Convert recorded interactions to automated steps
├── selector_memory.py       ← SQLite store for successful selector history
├── recovery_recipe_store.py ← SQLite store for full recovery action sequences
├── store.py                 ← RunStore — CRUD + SQLite persistence for RunState
├── test_case_store.py       ← TestCaseStore — CRUD + SQLite for test cases/folders
├── suite_store.py           ← SuiteStore — CRUD for suite run state
├── suite_executor.py        ← SuiteExecutor — batch test case execution
├── report_builder.py        ← Build HTML report + text summary for completed runs
├── step_importer.py         ← Parse CSV/XLSX uploads into step lists
├── viewer_session.py        ← ViewerSessionManager — VNC session per run
├── recovery/
│   ├── __init__.py
│   └── snapshot.py          ← FailureContext, diff_recovery_snapshots, promote_interactions
└── selectors/
    ├── __init__.py
    └── utils.py             ← Selector manipulation utilities
```

### Sub-component: AgentExecutor

**Purpose:** Process all steps of a single run in order. Implements the full 7-layer execution pipeline for each step.

**Inputs:**
- `run_id` — which run to execute
- Live browser state via `BrowserMCPClient`
- DOM snapshots via `inspect_page()`
- Brain responses via `HttpBrainClient`

**Outputs:**
- Updated `RunState` + `StepRuntimeState` persisted to `RunStore`
- Artifacts written to disk (step logs, screenshots, HTML report)
- Selector successes written to `SelectorMemoryStore`
- Recovery recipes written to `RecoveryRecipeStore`

**Execution pipeline per step (in order):**

```
1. Template rendering      TemplateEngine.render() — substitute {{variables}}
2. Recipe replay           RecoveryRecipeStore → try known-good action sequence
3. Semantic routing        SemanticTarget → find_best_match_for_target() (ARIA-only)
4. Perception match        inspect_page() → build_element_index() → find_best_match()
5. Selector memory         SelectorMemoryStore → try historical selectors
6. LLM selector direct     Use original selector from plan directly
7. Retry loop              Up to N attempts with delay
8. HITL pause              Pause run → user takes over → resume on confirmation
```

**Key methods:**

| Method | Description |
|---|---|
| `execute(run_id)` | Public entrypoint. Acquires per-run lock, delegates to `_execute_locked` |
| `_execute_locked(run_id)` | Full run lifecycle: navigate → step loop → report → summary |
| `_execute_existing_steps(run)` | Process all steps already in the run's step list |
| `_execute_step(run, step)` | Execute one step through the full 7-layer pipeline |
| `_try_perception_match(step, intent)` | Attempt DOM-first element identification |
| `_try_recipe_replay(run, step)` | Attempt automated replay of stored recovery recipe |
| `_try_selector_memory(domain, step_type, key)` | Look up historical successful selectors |
| `apply_manual_selector_hint(run_id, step_id, selector)` | Accept human-provided selector |
| `apply_human_recovery_confirmation(run_id, step_id)` | Mark step human_recovered, resume |

---

### Sub-component: Perception Engine

**Purpose:** Identify the correct DOM element by observing the live page, not by trusting the LLM's selector guess.

**Files:** `runtime/perception.py`

**Inputs:**
- `inspect_page()` snapshot (full ARIA tree + DOM attributes)
- Step intent text (what action to perform)
- Step type (click, type, select…)

**Outputs:**
- `PerceptionMatch` — best matching element with its derived selector and confidence level

**How it works:**

1. Parse every interactive element from the snapshot into `IndexedElement` objects (tag, role, text, aria-label, id, data-testid, placeholder, label, visible, enabled, derived selectors)
2. Build `ElementIndex` — deduplicated, sorted by selector stability
3. Score each element against the step's intent:
   - **Exact text match** — high weight
   - **ARIA label / accessible name match** — high weight
   - **Role match** — medium weight (button, textbox, combobox…)
   - **Placeholder match** — medium weight
   - **ID / testid presence** — stability bonus
4. Assign confidence: `unique` (only 1 element above threshold), `high` (clear winner), `medium`, `ambiguous`
5. Return `PerceptionMatch` with best selector

**Selector stability ranking (best to worst):**

```
0  #id                  — most stable
1  [data-testid]        — test-specific attributes
2  [aria-label]         — ARIA label
3  tag[name=]           — form field name attribute
4  [placeholder]
5  button:has-text()    — semantic text on buttons/links
6  text=               — text content
7  everything else      — least stable
```

---

### Sub-component: Intent Builder

**Purpose:** Extract structured semantic intent from a step definition so perception and matching have richer signal than just raw CSS.

**Files:** `runtime/intent_builder.py`

**Inputs:** A step dict + `SemanticTarget` (if present)

**Outputs:** `StepIntent` — element type, target text, accessible name, ordinal, scope hint

**Key judgments it makes:**
- Is this a textbox, button, combobox, link, or checkbox?
- Is there an ordinal hint ("second input", "first button")?
- Is there a container/scope hint ("login form", "modal dialog")?
- Does this step have enough semantic information to skip CSS-based matching entirely?

---

### Sub-component: HITL System

**Purpose:** Allow a human to take over a stuck step in the live browser, then resume automated execution.

**Files:** `runtime/hitl_manager.py`, `runtime/hitl_replayer.py`

**Flow:**

```
Step fails (all automatic recovery exhausted)
       │
       ▼
HitlManager.start_recording(run_id)
   → injects JS recorder into live page
   → recorder captures: click, dblclick, input, change, select events
       │
       ▼
Run status → waiting_for_input (user_input_kind = "recovery_confirm")
       │
       ▼  [user interacts with live browser via VNC viewer]
       │
HitlManager.read_interactions(run_id)
   → polls JS buffer every 3 seconds
   → returns list of {type, selector, value, timestamp}
       │
       ▼
User clicks "Resume" → POST /recovery-confirm
       │
       ▼
HitlManager.stop_recording(run_id)
HitlReplayer.normalize_interactions(events)
   → deduplicates rapid events
   → converts to clean {type, selector, value} step objects
       │
       ▼
Step marked human_recovered
Run resumes from next step
       │
       ▼
Interactions promoted → RecoveryAction → RecoveryRecipe → saved
```

---

### Sub-component: Recovery System

**Purpose:** Automatically replay previously successful recovery actions on future runs, reducing human intervention over time.

**Files:** `runtime/selector_memory.py`, `runtime/recovery_recipe_store.py`, `runtime/recovery/snapshot.py`

**Two layers:**

**Layer 1 — Selector Memory** (fast, selector-level):
- Stores: `(domain, step_type, element_key) → [selector, selector, ...]`
- Used: when LLM selector fails, try historical selectors before giving up
- Limitation: just a list of strings; no page context

**Layer 2 — Recovery Recipes** (complete, action-level):
- Stores: `RecoveryRecipe { domain, step_type, element_key, failure_context, actions[] }`
- Each `RecoveryAction` has: `action_type`, `label`, `role`, `selector_chain`, `value`
- `FailureContext` encodes: URL path, visible error text, page region
- Used: replay entire action sequence when step fails and failure context matches
- Benefit: works even when the selector itself changes, as long as the element's semantic identity is stable

**Recipe matching logic:**
1. Look up recipes by `(domain, step_type, element_key)`
2. Score each recipe's `FailureContext` against current page state
3. Select highest-scoring recipe whose context is compatible
4. Execute `actions[]` in sequence
5. Check `SuccessSignals` to verify the recipe worked

---

## Component 4: Backend — Browser Client

**Purpose:** All browser automation. The only component that talks to Playwright.

**Files:** `backend/app/mcp/browser_client.py` (~1,400 lines), `backend/app/mcp/filesystem_client.py`

**Inputs:** High-level commands from executor (`click`, `type`, `select`, `drag`, etc.)

**Outputs:** Browser state changes. `inspect_page()` DOM snapshots. Screenshots. Interaction recordings.

**Key behaviors:**

| Action | Implementation Notes |
|---|---|
| `navigate()` | `page.goto()` with retry and load-state wait |
| `click()` | Tries selector → falls back to text= match → JS `dispatchEvent` |
| `type_text()` | `fill()` for inputs, `type()` for contenteditable. Clears first if `clear_first=True` |
| `select_option()` | `page.select_option()` by value, then by label text, then by visible text |
| `drag_and_drop()` | Strategy cascade: native → mouse events → JS DataTransfer |
| `inspect_page()` | Returns full ARIA/DOM snapshot with all interactive elements, their attributes, and pre-computed selectors |
| `screenshot()` | `page.screenshot()` — saves to artifacts |
| `start_interaction_recording()` | Injects JS via `page.evaluate()` — attaches event listeners, stores events in `window.__phantomRecorder` buffer |
| `get_recorded_interactions()` | `page.evaluate()` — reads and clears `window.__phantomRecorder` buffer |

**Drag strategy decision tree:**

```
drag_and_drop(source, target)
    │
    ├─ Strategy 1: page.drag_and_drop(source, target)
    │      ↓ if TimeoutError or element not found
    ├─ Strategy 2: manual mouse events
    │      mousedown(source) → mousemove(steps) → mouseup(target)
    │      ↓ if still fails
    └─ Strategy 3: JS DataTransfer
           dispatchEvent(dragstart) → dispatchEvent(drop)
           ↓ if all fail
         raise last exception
```

---

## Component 5: Brain Service

**Purpose:** All LLM calls. Provider selection. Returns structured plan or step list to backend. Has no knowledge of browser state.

**Technology:** FastAPI, Python 3.11, OpenAI SDK (also supports Anthropic SDK, local vLLM).

**Files:**

```
brain/app/
├── main.py                    ← FastAPI app: /plan, /human_steps, /summarize, /health
├── config.py                  ← Settings: LLM_MODE, API keys, model names
├── schemas.py                 ← Request/response Pydantic models
└── llm/
    ├── base.py                ← LLMProvider abstract base class
    ├── factory.py             ← build_llm_provider() — reads LLM_MODE, returns correct provider
    ├── anthropic_provider.py  ← Claude via Anthropic SDK
    ├── openai_provider.py     ← GPT-4o etc. via OpenAI SDK
    ├── local_vllm.py          ← Local vLLM OpenAI-compatible endpoint
    └── utils.py               ← Shared prompt utilities, token counting
```

**Endpoints:**

| Endpoint | Input | Output |
|---|---|---|
| `POST /plan` | Task text + constraints | `{ "steps": [...] }` — JSON step list |
| `POST /human_steps` | Task text | `["step 1", "step 2", ...]` — plain-English action lines |
| `POST /summarize` | Run result text | Short natural language summary of the run |
| `GET /health` | — | `{ "mode": "cloud", "model": "gpt-4o", "status": "ok" }` |

**LLM provider abstraction:**

```python
class LLMProvider(Protocol):
    async def complete(self, messages: list[dict], max_tokens: int) -> str: ...
    async def healthcheck(self) -> dict: ...
```

`factory.py` reads `LLM_MODE` env var and returns the right implementation. Swapping the LLM requires only changing `LLM_MODE` — no backend changes needed.

---

## Component 6: Data Layer

**Purpose:** Persist run state, test cases, selector memory, and recovery recipes across server restarts.

**Technology:** SQLite (three separate databases). SQLAlchemy for the auth database. Raw `sqlite3` for operational stores.

**Databases:**

| Database | File | Contents | Managed By |
|---|---|---|---|
| `auth.db` | `data/auth.sqlite3` | Users, refresh tokens | SQLAlchemy models |
| `run_store` | `data/run_store.sqlite3` | Serialized `RunState` JSON blobs | `RunStore` class |
| `selector_memory` | `data/selector_memory.sqlite3` | `(domain, step_type, key) → selector[]` | `SelectorMemoryStore` |
| `recovery_recipes` | `data/recovery_recipes.sqlite3` | `RecoveryRecipe` JSON blobs | `RecoveryRecipeStore` |

**Artifact storage:**

All run artifacts (step logs, screenshots, HTML reports, summary text) are written to:
```
artifacts/
└── <run_id>/
    ├── step-000.log
    ├── step-001.log
    ├── step-002-failed.png
    ├── report.html
    └── summary.txt
```

These are served by the backend via `GET /api/artifacts/{run_id}/{filename}`.

---

## Component 7: Infrastructure

**Purpose:** Networking, routing, containerization, CI.

**Files:**

```
docker-compose.yml          ← Production: backend + brain + frontend + nginx
docker-compose.dev.yml      ← Dev: same services with volume mounts + hot reload
nginx/default.conf          ← Reverse proxy routing rules
backend/Dockerfile
brain/Dockerfile
frontend/Dockerfile
dev.bat                     ← Windows local startup script
backend/start_with_display.sh ← Linux VNC session startup
.github/workflows/
├── backend-ci.yml          ← pytest + lint on every PR
└── frontend-ci.yml         ← eslint + next build on every PR
```

**Nginx routing:**

```
/          → frontend:3000
/api/*     → backend:8080
/auth/*    → backend:8080
/viewer/*  → backend:8080 (VNC proxy)
```

**VNC viewer (Linux only):**
- Each run that requests viewer support gets its own Xvfb virtual display
- Chromium runs on that display
- x11vnc serves the display on a dedicated port
- Nginx proxies NoVNC WebSocket to that port
- URL pattern: `/viewer/<run_id>?token=<viewer_token>`

---

## Data Flow: End-to-End

```
User types: "Go to app.example.com, log in as admin, verify dashboard"
       │
       ▼
Frontend: POST /api/plan { task: "...", start_url: "https://app.example.com" }
       │
       ▼
Backend: forward to Brain → POST /brain/human_steps → returns:
  ["Go to https://app.example.com",
   "Fill email field with admin@example.com",
   "Fill password field with admin123",
   "Click the Sign In button",
   "Verify Dashboard heading is visible"]
       │
       ▼
Backend: forward to Brain → POST /brain/plan → returns:
  { steps: [
    { type: "navigate", url: "https://app.example.com" },
    { type: "type", target: { semantic_name: "email", expected_role: "textbox" }, text: "admin@example.com" },
    { type: "type", target: { semantic_name: "password", expected_role: "textbox" }, text: "admin123" },
    { type: "click", target: { semantic_name: "sign in button", expected_role: "button" } },
    { type: "verify_text", value: "Dashboard" }
  ]}
       │
       ▼
Backend: _sanitize_plan_steps() → _validate_generated_plan() → RunCreateRequest validated
       │
       ▼
RunStore.create() → run_id assigned, status=pending
background_task: executor.execute(run_id)
       │
       ▼  [Per step]
executor: TemplateEngine.render() → recipe replay? → semantic routing?
       → perception: inspect_page() → build_element_index() → find_best_match_for_target()
       → BrowserMCPClient.type_text(selector, "admin@example.com")
       → StepRuntimeState.status = completed
       │
       ▼  [After all steps]
build_html_report() → artifacts/run_id/report.html
Brain.summarize() → artifacts/run_id/summary.txt
RunState.status = completed
       │
       ▼
Frontend: polling GET /api/runs/{run_id} → shows completed steps
```
