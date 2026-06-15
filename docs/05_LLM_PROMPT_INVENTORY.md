# Document 5: LLM Prompt Inventory

> **Purpose:** Document every prompt the system sends to an LLM — the exact text, why each constraint exists, what the expected output format is, and what breaks if the prompt changes.  
> **Why this matters:** The prompts embody months of debugging. A single removed constraint can reintroduce bugs that took weeks to find. This document is the source of truth for prompt intent.

---

## Overview: All LLM Calls

| # | Endpoint | Brain Route | Called From | Purpose | Output Format |
|---|---|---|---|---|---|
| 1 | Planning | `POST /v1/plan` | `backend/main.py generate_plan()` | Convert task + constraints into executable step JSON | `{ "run_name", "start_url", "steps": [...] }` |
| 2 | Step Expansion | `POST /v1/human-steps` | `backend/main.py generate_plan()` | Expand free-form prompt into numbered plain-English action list | `["step 1", "step 2", ...]` |
| 3 | Summarize | `POST /v1/summarize` | `executor._execute_locked()` after run ends | One-sentence natural language summary of the run | Plain text string |
| 4 | Selector Repair | `POST /v1/selector-suggestions` | `executor` on step failure | Suggest alternative CSS selectors when a step's selector failed | `{ "selectors": ["...", "..."] }` |
| 5 | Failure Diagnosis | `POST /v1/diagnose-failure` | `executor` on hard failure | Vision: look at a screenshot and explain what went wrong | `{ "diagnosis": "...", "suggested_fix": "..." }` |

---

## Prompt 1: Plan Generation (`/v1/plan`)

### Where It's Used
- `brain/app/llm/anthropic_provider.py` — `plan_task()`
- `brain/app/llm/openai_provider.py` — `plan_task()`
- Both use identical system prompts.

### System Prompt (exact text)

```
You are a web automation planner.
Return ONLY strict JSON with keys: run_name, start_url, steps.
steps may only use these step types: navigate, click, type, select, drag, scroll, wait, handle_popup, verify_text, verify_image.

CRITICAL: Preserve every instruction in the task list as a separate step.
Do NOT merge, combine, or skip any numbered instruction.
Each numbered instruction must map to at least one step in your output.
Cover every explicit user instruction in order when max_steps allows.
Do not invent extra requirements not present in the task.

TRACEABILITY: Add a 'src_step' field (integer) to every step.
Set it to the 1-based index of the numbered instruction that step implements.
If one instruction expands into multiple steps, all of them share the same src_step value.

SEMANTIC TARGET CONTRACT: For every click, type, and select step include a 'target' object
that describes the element by its semantic identity only — never by its DOM structure.
Set 'target.semantic_name' to a concise canonical name for the element
(e.g. 'email', 'password', 'confirm password', 'submit button', 'country').
Set 'target.expected_role' to the ARIA semantic role:
textbox, button, combobox, link, checkbox, radio, menuitem, tab, switch, or option.
Set 'target.accessible_name' to the exact visible label text, button text, link text,
or accessible name the user would read (e.g. 'Email Address', 'Sign In', 'Country').
Set 'target.placeholder' only if the element has visible placeholder text; otherwise omit it.
Set 'target.scope' if the element belongs to a named section or form
(e.g. 'login form', 'registration form', 'search bar', 'navigation').
Do NOT include a 'selector' field in any step.
Do NOT generate CSS selectors, XPath, nth-child paths, or any DOM traversal string.
The runtime will locate the element from the semantic description at execution time.
Omit target fields you are not confident about.
```

### User Message (exact text)

```
Task: {task}
Max steps: {max_steps}
Return compact valid JSON only.
Every numbered instruction must appear as at least one step.
Include src_step and target in every click, type, and select step.
```

### Why Each Constraint Exists

| Constraint | Added Because | What Breaks Without It |
|---|---|---|
| "Return ONLY strict JSON" | LLM would wrap output in markdown code blocks or add explanatory text, breaking JSON parsing | `extract_json_object()` fails, fallback plan used |
| "steps may only use these step types" | LLM invented step types like `assert`, `input`, `verify` that the executor doesn't understand | Plan normalizer drops all unknown-type steps, plan becomes empty |
| "CRITICAL: Do NOT merge, combine, or skip" | LLM would collapse "click Submit, then wait for dashboard" into one `click` step, silently dropping the wait | Flaky tests: steps that depend on load time fail intermittently |
| "Do not invent extra requirements" | LLM would add login steps to tasks that didn't ask for login, or add verification steps for every action | Plans ran extra unnecessary steps, confusing the user |
| "src_step field" | Without traceability, there was no way to know which instruction produced which action when a step failed | Debug logs couldn't be correlated back to user instructions |
| "SEMANTIC TARGET CONTRACT" | LLM was generating brittle CSS selectors (`div.MuiButtonBase-root`) that broke on every page reload | Wrong element clicks, type into wrong field — the core reliability problem |
| "Do NOT include a 'selector' field" | Even after adding semantic target, LLM would still include `selector` fields alongside `target`. Executor would use the brittle selector first | Perception bypassed; brittle selector failure |
| "Do NOT generate CSS selectors, XPath" | Without this explicit prohibition, LLM routinely generated nth-child paths, class-based selectors, and jQuery-style selectors | Selectors broke on every app that used dynamic class generation |
| "Omit target fields you are not confident about" | LLM would fill in guessed values for `accessible_name` like "button" or "input" — generic and worse than nothing | Low-quality targets reduced perception accuracy |

### Additional Constraints Applied in `backend/main.py`

The backend adds extra constraints to the planning task text before sending to brain. These are appended to the task description itself (not the system prompt):

```
Planner constraints:
- Return only runnable steps supported by this runtime.
- Allowed step types: navigate, click, type, select, drag, scroll, wait, handle_popup, verify_text, verify_image.
- For drag-and-drop: use type='drag' with 'source_selector' and 'target_selector'.
- Cover every explicit user instruction in order when max_steps allows.
- Do not invent extra requirements not explicitly requested.
- Use Playwright-compatible selectors only.
- Never use jQuery ':contains(...)'. Use 'text=...' or ':has-text("...")' instead.
- Prefer stable selectors: id, name, data-testid, role/label-based selectors.
- Avoid brittle selectors such as exact list indexes ('data-index=0', ':first-child').
- Do not use generic verification selectors like 'h1' or 'body'.
- For checks like 'Create Form button is visible', use button selectors with id/name or :has-text.
- Keep action order aligned to the prompt.
- For login pages, prefer '#username' or input[name='username'] for username/email fields.
- CRITICAL: Each selector field must contain exactly ONE selector string. Never use comma-separated
  fallback lists (e.g. 'a, b, c'). Pick the single best selector.
```

| Constraint | Added Because |
|---|---|
| "Never use jQuery ':contains(...)'" | LLM would generate jQuery-specific selectors that Playwright does not support |
| "Do not use generic verification selectors like 'h1' or 'body'" | LLM used `h1` as a verification target — it always matched something, giving false passes |
| "Each selector field must contain exactly ONE selector string" | LLM would output `"button.primary, #submit, [type='submit']"` as one selector. Executor doesn't split these, so all three were sent to Playwright as a single invalid selector |
| "For login pages, prefer '#username'" | Login form fields have specific known-good selectors. Without this hint, the LLM would generate dynamic class selectors that broke on every app |

### Token Budget
- `max_output_tokens`: **1400** (OpenAI) / **8192** (Anthropic)
- Note: The 1400-token limit for OpenAI can cause truncated plans on tasks with 20+ steps. Anthropic's 8192 limit is not a practical constraint.

### Fallback Behavior
If the LLM returns empty output or invalid JSON: `fallback_plan()` is called. It returns a two-step plan (wait + verify) that will almost certainly fail on the real page. The `raw_llm_response` field is preserved for debugging.

---

## Prompt 2: Step Expansion (`/v1/human-steps`)

### Where It's Used
`backend/main.py generate_plan()` — called before planning to pre-expand the raw task into numbered action lines.

### Purpose
Prevent the planner from merging multiple actions into one step. By first converting the free-form task into an explicit numbered list, every intended action has its own line before the planner converts them to JSON.

### System Prompt (exact text, Anthropic)

```
You break a web automation task into short, clear steps.
Each step is one short sentence describing exactly one action.
Use simple words. Name the element by its visible label. Include the value if relevant.
Example good steps:
  'Go to https://example.com',
  'Click the Search button',
  'Type "hello" in the search box',
  'Verify the heading says "Welcome"'.
Return ONLY a JSON array of strings. No extra explanation.
```

### User Message

```
Task: {prompt}
Max steps: {max_steps}
Return the JSON array.
```

### Why Each Constraint Exists

| Constraint | Reason |
|---|---|
| "Each step is one short sentence describing exactly one action" | Without this, LLM would produce compound sentences: "Click Submit and wait for the dashboard to load" — which the planner would then collapse back to one step |
| "Name the element by its visible label" | Forces the LLM to use human-readable names ("Sign In button") not DOM names ("#btn-9201"). These names feed into the planning prompt which then produces better `accessible_name` values |
| "Return ONLY a JSON array of strings" | LLM would wrap in markdown, number the items, or add introductory text |

### Token Budget
- `max_output_tokens`: **2000** (Anthropic)
- Capped at **30 steps** in the backend call: `brain_client.human_steps(request.task, max_steps=30)`

### Fallback Behavior
If parsing fails or LLM returns empty: returns `[prompt]` — the original raw text as a single-item list. Planning then proceeds with the raw prompt as if `human_steps` was never called.

---

## Prompt 3: Run Summarize (`/v1/summarize`)

### Where It's Used
`executor._execute_locked()` — called after every run completes (success or failure).

### System Prompt (exact text)

```
Summarize this automation run in one concise sentence.
```

### User Message
The full run result text (step statuses, messages, timing) up to 3000 characters.

### Why This Exists
The run result JSON is machine-readable but not human-readable. The summary is shown in the UI run list and written to `artifacts/<run_id>/summary.txt` for stakeholder review.

### Token Budget
- `max_output_tokens`: **80** (OpenAI) / **120** (Anthropic)

### Fallback Behavior
If LLM fails or returns empty: `"Run finished."` is used. The backend also appends the run name if it's missing from the summary: `f"{summary} Run: {run.run_name}."`.

---

## Prompt 4: Selector Repair (`/v1/selector-suggestions`)

### Where It's Used
`executor` — called when a step fails due to an element not being found, after perception and memory candidates are exhausted.

### Purpose
Ask the LLM to look at the live page state and suggest better CSS selectors for the failed element.

### System Prompt (exact text, Anthropic)

```
You repair failed web automation selectors.
Return ONLY strict JSON like {"selectors":["..."]}.

STRICT RULES — you MUST follow all of these:
1. Return ONLY plain CSS selectors or Playwright text selectors (e.g. text=Submit, button:has-text('Login')).
   NEVER return Playwright API calls like page.getByRole(), page.getByLabel(), page.locator(), await, or any JavaScript code.
2. Priority order for selector strategy:
   a. id attribute → use #id (e.g. #searchLanguage)
   b. name attribute → use [name='value'] (e.g. select[name='language'])
   c. data-testid or data-cy → use [data-testid='value']
   d. aria-label → use [aria-label='value']
   e. placeholder → use input[placeholder='value']
   f. visible text → use text=Label or button:has-text('Label')
   g. tag + class as last resort → use tag.classname
3. Use page.interactive_elements as the primary source.
4. Each selector must be independently usable in document.querySelector().
5. [If element hint available] The target element identity is known: {hint_clause} — prioritise matching these attributes.
```

### User Message
JSON object containing: `step_type`, `failed_selector`, `error_message`, `text_hint`, `element_hint` (fingerprint), `page` (DOM summary), `max_candidates`.

### Why Each Constraint Exists

| Constraint | Reason |
|---|---|
| "NEVER return Playwright API calls" | LLM would return `page.getByRole('button', {name: 'Login'})` — valid Playwright but not a CSS string. The executor expects a plain string it can pass to `page.locator()` |
| Explicit priority order (id > name > testid > aria > placeholder > text > class) | Without this, the LLM would often pick class-based selectors which break on class name changes. The priority order matches the `_selector_stability_rank()` function in perception.py |
| "Each selector must be independently usable in document.querySelector()" | LLM would produce chained or context-dependent selectors that look valid but can't be used standalone |

### Token Budget
- `max_output_tokens`: **220** (OpenAI) / **300** (Anthropic)

### Fallback Behavior
If LLM returns empty or invalid JSON: returns `[]`. The executor proceeds without LLM suggestions.

---

## Prompt 5: Failure Diagnosis (`/v1/diagnose-failure`)

### Where It's Used
Called on hard step failures. **Vision-capable call** — passes a base64 screenshot alongside the error message.

### Purpose
Look at the actual browser screenshot at the moment of failure and explain in human language what went wrong. Helps the user understand failures without reading raw error messages.

### System Prompt (exact text)

```
You are a QA automation assistant.
Given a browser screenshot at the moment a test step failed,
return ONLY a JSON object with keys 'diagnosis' and 'suggested_fix'.
Be specific about what you see on the page. No markdown, no extra keys.
```

### User Message
Multipart: a base64-encoded PNG screenshot + text:

```
A web automation step of type '{step_type}' just failed. [The overall goal was: {goal}.]
Error: {error_message}

Look at the screenshot and respond with ONLY strict JSON:
{"diagnosis": "1-2 sentences describing what went wrong based on what you see on the page",
 "suggested_fix": "1 sentence on what the automation should do differently"}
```

### Why Each Constraint Exists

| Constraint | Reason |
|---|---|
| Vision input (screenshot) | Text-only diagnosis is often misleading — "element not found" could mean a CAPTCHA appeared, the page is loading, an error modal blocked the element, or the URL was wrong. Screenshot makes these instantly obvious |
| "Be specific about what you see on the page" | Without this, LLM would give generic answers like "The element may not be visible" — identical to the raw error message, adding no value |
| `temperature: 0` | Diagnosis should be deterministic — same page state → same diagnosis. Random variation makes debugging harder |

### Token Budget
- `max_output_tokens`: **300**
- Screenshot is passed as PNG base64 — roughly 100-300KB per capture depending on page complexity

### Availability
- **Anthropic only** — implemented in `AnthropicProvider.diagnose_failure()`. OpenAI provider does not have this endpoint.
- Falls back to `{ "diagnosis": "Step failed: {error}", "suggested_fix": "Check the selector or page state and retry." }` if API key is missing or call fails.

---

## Prompt Interaction Map

These prompts are called in this order during a standard plan-and-execute flow:

```
User submits task
    │
    ▼
[1] POST /v1/human-steps  ← expand raw prompt to numbered steps
    │
    ▼
[1] POST /v1/plan         ← convert numbered steps to JSON step plan
    │
    ▼  (plan runs, steps execute)
    │
    ├── step fails ──►
    │       [4] POST /v1/selector-suggestions  ← suggest better selectors
    │       [5] POST /v1/diagnose-failure      ← vision: explain failure
    │
    ▼
[3] POST /v1/summarize    ← summarize completed run
```

---

## What to Never Change Without Testing

These prompt elements are load-bearing — changing them has a high probability of breaking something:

| Element | Risk If Changed |
|---|---|
| "Do NOT include a 'selector' field in any step" | Executor will use brittle LLM selector instead of perception-derived one |
| "Do NOT generate CSS selectors, XPath, nth-child paths" | Regression to the Phase 0 brittleness problem |
| `src_step` requirement | Step trace logs lose traceability, debugging becomes much harder |
| ARIA role list (`textbox, button, combobox, link, checkbox, radio...`) | LLM invents non-standard roles that don't match ARIA spec. Perception engine can't match them |
| `max_output_tokens: 1400` (OpenAI plan) | Increasing risks more verbose output that hits JSON parsing issues. Decreasing truncates complex plans |
| Selector priority order in prompt 4 | Perception and selector repair would use different stability rankings, producing inconsistent behavior |
