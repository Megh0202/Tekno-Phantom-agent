# Document 4: Glossary of Project-Specific Terms

> **Purpose:** Define every term used in this project that is not standard industry vocabulary.  
> If a word appears in the other documents and you're not sure what it means, look here first.

---

## A

### Accessible Name
The text a screen reader announces for an element. For a button with text "Sign In", the accessible name is "Sign In". For an input with `<label>Email Address</label>`, the accessible name is "Email Address". The perception engine scores elements partly by how well the accessible name matches the step's target. Set in `SemanticTarget.accessible_name`.

### Action Plan / Plan
The ordered list of steps the system will execute. Generated either by the LLM (via `/v1/plan`), by the structured parser (rule-based, no LLM), or authored manually in the UI. Stored as a list of step dicts inside a `RunState`.

### Artifact
Any file written to disk during or after a run. Stored in `artifacts/<run_id>/`. Includes: `step-000.log`, `step-001-failed.png`, `report.html`, `summary.txt`. Served by the backend at `GET /api/artifacts/{run_id}/{filename}`.

### Autonomous Mode
A run execution mode where the executor generates new steps mid-run if the current step list is exhausted and a `prompt` is set on the run. The LLM is called in a loop to decide the next action based on current page state. Contrast with the default mode where all steps are fixed at run creation.

---

## B

### Brain Service
The standalone LLM microservice (`brain/`). Runs on port 8090. Accepts task descriptions, returns JSON step plans or plain-English step lists. Owns all LLM provider configuration. The backend never calls an LLM directly — it always goes through the Brain service. Swapping LLM providers requires only changing `LLM_MODE` in the brain service.

### Browser Client
`BrowserMCPClient` in `backend/app/mcp/browser_client.py`. The only component that talks directly to Playwright. All browser interactions (navigate, click, type, drag, screenshot, DOM snapshot) go through this class.

---

## C

### Candidate Confidence
The confidence level assigned to an element after perception scoring. Four levels:
- `unique` — only one element scored above the threshold. Execute immediately.
- `high` — one element clearly outscores all others. Execute directly.
- `medium` — one element has the best score but others are close. Try, with fallback.
- `ambiguous` — multiple elements score similarly. Do not execute without more signal.

### CSS Selector
A text pattern used to locate an element in the DOM (e.g., `#email`, `.btn-primary`, `input[name="password"]`). In early versions of this project, the LLM generated CSS selectors directly. This was the primary source of failures. The current system avoids CSS selectors in the LLM plan entirely — they are derived from the live DOM by the perception engine at execution time.

---

## D

### DOM Snapshot / `inspect_page()`
A full read of the current page's interactive elements, taken by calling `inspect_page()` on the browser client. Returns a structured list of elements, each with: tag, role, visible text, ARIA label, ID, data-testid, placeholder, label, enabled/visible state, and a list of pre-computed selectors. Used by the perception engine to build the element index.

### Drag Debug Log
`data/drag_debug.jsonl` — a JSON lines file written during every drag operation. Each line records: the source/target selectors, which strategy was tried, whether it succeeded, coordinates used, and the time elapsed. Used to diagnose drag failures without needing to re-run.

---

## E

### Element Fingerprint / Signature
A stable identity snapshot of a DOM element stored in the selector memory database. Contains: tag, role, visible text, ARIA label, name attribute, ID, data-testid, placeholder. Unlike a CSS selector, a fingerprint survives minor DOM changes because it describes the element semantically, not structurally.

### Element Index (`ElementIndex`)
The output of `build_element_index()`. A structured, deduplicated collection of all visible interactive elements on the current page, each parsed into an `IndexedElement`. Built fresh from a DOM snapshot before each step. The perception engine scores elements in this index against the step's intent.

### Element Key
A stable string derived from `(step_type, selector_or_semantic_info)` used to index into the selector memory and recipe stores. The key is designed to remain the same across runs on the same app even if the exact CSS selector changes. Computed by `AgentExecutor._extract_element_key()`.

### Execution Lock
A per-run `asyncio.Lock` that prevents two concurrent execution calls for the same run from racing. Acquired at the start of `AgentExecutor.execute()`. Needed because HITL recovery can trigger a second `execute()` call while the first is still finishing.

---

## F

### Failure Context (`FailureContext`)
A structured record of the page conditions at the moment a step failed. Includes: URL path, visible error text, page region, missing element description. Stored alongside recovery recipes so they are only replayed when the page conditions match the original failure.

### Fallback Plan
A minimal safe plan returned by the brain service when LLM generation fails or returns unusable output. Contains one `wait` step and one `verify_text` step pointing at a generic `h1`. It will almost always fail, but it prevents the system from crashing entirely.

### Fast Path
An execution optimisation in the executor. If a step's selector resolves in under 2 seconds on the first try, the result is returned immediately without waiting for the full timeout. Controlled by `EXECUTION_FAST_PATH_ENABLED` and `EXECUTION_FAST_PATH_ACTION_TIMEOUT_SECONDS`.

### Folder (Test Folder)
A organisational container for test cases. Nested hierarchy supported. Deleting a folder cascades to all child folders and their test cases.

---

## H

### HITL (Human-in-the-Loop)
The recovery mechanism where the system pauses a stuck run, keeps the browser alive, and allows a human to perform the blocked action manually in the live browser. The human's interactions are recorded by a JS recorder, normalized into steps, and replayed on future runs. HITL is the last layer of recovery — when all automated options are exhausted. Implemented in `hitl_manager.py` and `hitl_replayer.py`.

### HITL Recorder
A JavaScript snippet injected into the live page via `page.evaluate()` during a HITL session. Attaches passive event listeners for `click`, `dblclick`, `input`, `change`, and `select` events. Stores captured events in `window.__phantomRecorder` buffer. Does not interfere with the page's own event handlers.

### Human Steps (`/v1/human-steps`)
A brain service endpoint that expands a free-form task description into a numbered list of plain-English action sentences. Called before plan generation to pre-expand the task into explicit individual steps, preventing the planner from merging two actions into one step.

---

## I

### IndexedElement
A single interactive element parsed from a DOM snapshot. Immutable dataclass. Contains: tag, role, text, aria label, name, ID, testid, placeholder, input type, associated label text, visible/enabled flags, and an ordered tuple of selectors (most-stable first).

### Intent Builder
`intent_builder.py`. Converts a raw step dict into a structured `StepIntent` — a richer semantic description of what the step is trying to do. Used to give the perception engine more signal than just a CSS selector string. Extracts: element type (textbox, button, combobox), target text, accessible name, ordinal hint (1st, 2nd), scope hint (login form, modal).

---

## M

### Mock Mode
`BROWSER_MODE=mock`. A browser client implementation that logs actions without touching a real browser. Used for unit tests and development when a browser isn't available. All actions succeed by default.

---

## P

### Perception Engine
`perception.py`. The component that identifies the correct DOM element by observing the live page, scoring every element against the step's intent, and deriving a fresh CSS selector from the best match. The fundamental shift from "trust the LLM's selector" to "find the element from the live DOM". Added in Phase 4 (April 8, 2026).

### Perception Match (`PerceptionMatch`)
The result object returned by `find_best_match()`. Contains: the matched `IndexedElement`, the best selector to use, the score, the confidence level, and the number of alternative elements that also scored above threshold.

### Plan Mode vs. Autonomous Mode
Two execution modes for a run:
- **Plan mode** (default): All steps are fixed at run creation. The executor runs them in order.
- **Autonomous mode**: A `prompt` is set on the run. The executor generates steps on-the-fly, calling the LLM after each completed step to decide the next action.

### Plan Normalizer
`plan_normalizer.py`. Converts raw LLM output (which may use inconsistent field names, wrong type names, or string steps) into the canonical step schema the executor understands. Called on every plan before execution.

### Plan Trace
A JSON file written to `artifacts/plan-<timestamp>/plan-trace.json` during plan generation. Records all planning attempts, the structured parser result, the final normalized steps, and validation results. Used to debug why a plan looks different from the task description.

---

## R

### Recipe / Recovery Recipe (`RecoveryRecipe`)
A stored record of a complete sequence of actions that successfully recovered from a specific failure. Contains: domain, step type, element key, failure context, and a list of `RecoveryAction` objects. When the system encounters a matching failure on a future run, it replays the recipe automatically instead of failing.

### Recovery Action (`RecoveryAction`)
One action within a recovery recipe. Types: `fill_field` (type text into an input), `click_control` (click a button or link), `select_option` (choose a dropdown option). Contains enough semantic info (label, role, selector chain) to be executed without relying on exact CSS.

### Recovery Confirm
The HTTP endpoint `POST /api/runs/{run_id}/steps/{step_id}/recovery-confirm`. Called by the UI after the user has performed the blocked action manually during a HITL session. Triggers normalization of the recorded interactions, marks the step as `human_recovered`, and resumes the run.

### Run
A single execution of a test scenario. Has a unique `run_id`. Contains: start URL, ordered steps, execution mode, test data, status, and references to artifacts. The core unit of work in the system.

### Run Store (`RunStore`)
SQLite-backed persistence for `RunState` objects. Stores each run as a serialized JSON blob. In-memory mode also available for tests.

---

## S

### Selector Memory (`SelectorMemoryStore`)
A SQLite database that remembers which CSS selectors succeeded for each `(domain, step_type, element_key)` combination. When a step fails, the executor queries this store for historical candidates before giving up. Written to `data/selector_memory.sqlite3`.

### Selector Profile
A `dict[str, list[str]]` stored on a test case. Maps semantic keys (e.g., `"login_button"`, `"email"`) to lists of CSS selectors known to work for that element on the target app. Used by the template engine to resolve `{{selector.login_button}}` references in step definitions.

### Selector Recovery
The HITL-lite mechanism where a paused run asks the user to provide the correct CSS selector for the failed element (rather than performing the action manually). The selector is submitted via `POST /api/runs/{run_id}/steps/{step_id}/selector`, remembered in `SelectorMemoryStore`, and the run resumes.

### Semantic Routing
A fast path in the executor for steps with a complete `SemanticTarget`. Instead of going through perception scoring, the step is sent directly to `find_best_match_for_target()` which scores elements purely by ARIA role and accessible name. Bypasses brittle CSS matching entirely. Only available when `semantic_name + expected_role + accessible_name` are all set.

### SemanticTarget
A Pydantic model describing an element by its semantic identity rather than its DOM structure. Fields:
- `semantic_name` — canonical element name (e.g., `"email"`, `"submit button"`)
- `expected_role` — ARIA role (textbox, button, combobox, link, checkbox, radio...)
- `accessible_name` — visible label/button text (e.g., `"Email Address"`, `"Sign In"`)
- `scope` — container name (e.g., `"login form"`, `"navigation"`)
- `placeholder` — visible placeholder text if present

Legacy fields (`kind`, `role`, `text`, `label`, `context`) are kept for backward compatibility.

### Src Step
A `src_step` integer field the LLM adds to every generated step. Records which numbered instruction in the task description the step implements (1-based index). Used by `_build_step_source_map()` to produce a traceability map: "action[3] came from instruction[2]".

### Step
One atomic browser action within a run. Has a type (`click`, `type`, `navigate`, etc.), input parameters, a `step_id`, and a `StepRuntimeState` tracking execution status, timestamps, message, and artifact path.

### Step Intent (`StepIntent`)
The output of `intent_builder.build_step_intent()`. A structured semantic description of what a step is trying to do: element type, target text, accessible name, ordinal (1st/2nd), scope hint. Used by the perception engine as richer input than raw CSS.

### Structured Parser
`instruction_parser.py`. A rule-based (no LLM) parser that converts structured task descriptions directly into step lists. Only works on tasks that follow expected patterns (numbered lists, clear action verbs). Fast and deterministic. Used as the first attempt in `generate_plan` before calling the LLM.

### Suite
A named collection of test cases that are executed together as a batch. A suite run creates one `SuiteRunState` that tracks the overall status and links to individual `RunState` objects for each test case.

### Summary Text
A one-sentence natural language description of the run result, generated by `POST /v1/summarize` after the run completes. Written to `artifacts/<run_id>/summary.txt` and stored on `RunState.summary`.

---

## T

### Template Engine
`template_engine.py`. Performs `{{variable}}` substitution in step fields before execution. Two namespaces:
- `{{email}}`, `{{password}}` → replaced from `test_data` dict
- `{{selector.login_button}}` → replaced from `selector_profile` dict (resolves to a CSS selector)

### Test Case
A named, reusable test scenario stored in the system. Contains: name, description, start URL, ordered steps, test data, selector profile, and parent folder. Can be run on demand via `POST /api/test-cases/{id}/run`.

### Test Data
A `dict[str, str]` attached to a run or test case. Provides values for template placeholders (e.g., `{"email": "admin@example.com", "password": "secret123"}`). Values are substituted into step fields by the template engine before execution.

---

## V

### Viewer Session
A per-run VNC session that lets the user watch the browser executing in real time. Each run gets its own virtual display (Xvfb), Chromium process, and VNC server. Proxied through Nginx so the user sees it in an iframe at `viewer_url`. Linux/Docker only.

### vLLM
An open-source LLM inference engine. Used in `LLM_MODE=local` to run models (e.g., LLaMA) locally without sending data to OpenAI or Anthropic. The brain service calls it via the OpenAI-compatible API endpoint.

---

## W

### Waiting for Input (`waiting_for_input`)
A run status indicating the run is paused and waiting for human input. Two sub-types:
- `user_input_kind = "selector"` — waiting for the user to provide a CSS selector
- `user_input_kind = "recovery_confirm"` — waiting for the user to perform the action manually and click "Resume"

A 120-second timeout applies. If no input is received, the run transitions to `failed`.
