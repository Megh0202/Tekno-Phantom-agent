# Document 9: Metrics and Observability Guide

> **Purpose:** What does a healthy system look like? What log patterns mean what? What should you watch in production?  
> **How logs are structured:** All log lines follow this format (from `logging_config.py`):  
> `YYYY-MM-DD HH:MM:SS | LEVEL    | logger-name                             | message`

---

## 1. Logger Names (What Produces What)

Each subsystem has its own named logger. Filter by name to isolate issues.

| Logger Name | Source File | What It Logs |
|---|---|---|
| `tekno.phantom.api` | `main.py` | HTTP request handling, run creation, plan generation |
| `tekno.phantom.executor` | `executor.py` | All step execution — the most verbose and most useful logger |
| `tekno.phantom.browser` | `browser_client.py` | Every browser action (click, type, navigate, screenshot) |
| `tekno.phantom.brain` | `brain/http_client.py` | LLM requests and responses, timing |
| `tekno.phantom.hitl` | `hitl_manager.py` | HITL pause, recorder injection, recovery confirm |
| `tekno.phantom.replayer` | `hitl_replayer.py` | Recipe replay attempts, step-by-step replay actions |
| `tekno.phantom.perception` | `perception.py` | Element scoring, match results |
| `tekno.phantom.plan_normalizer` | `plan_normalizer.py` | Plan normalization, dropped steps, unknown types |
| `tekno.phantom.recipe_store` | `recovery_recipe_store.py` | Recipe save/load, context match scoring |
| `tekno.phantom.selector_memory` | `selector_memory.py` | Selector memory hits and writes |
| `tekno.phantom.viewer` | `viewer_session.py` | Xvfb/VNC process start, port allocation |
| `tekno.phantom.auth.service` | `auth/service.py` | Login, token issue, rate limit hits |
| `tekno.phantom.suite_executor` | `suite_executor.py` | Suite run progress, test case sequencing |

**Log location:** `backend/logs/YYYY-MM-DD.log` — one file per day, rolling at midnight. Also written to stdout (visible in Docker logs: `docker compose logs -f backend`).

---

## 2. What a Healthy Run Looks Like

A clean successful run produces this log sequence from `tekno.phantom.executor`:

```
INFO  | Run abc123: starting execution (mode=plan, steps=5, url=https://app.example.com)
INFO  | Run abc123: step 1/5 type=navigate selector=''
INFO  | Run abc123: step 1 done status=completed message='Navigated to ...'
INFO  | Run abc123: step 2/5 type=click selector='#login-button'
INFO  | Run abc123 step 2 (type=click): perception_probe target_present=True text_hint='Log In' selector='#login-button' → result=confidence=high score=82
INFO  | Run abc123 step 2 (type=click): perception GROUNDED selector='#login-button' (confidence=high score=82 alternatives=0 element='button' derived_selectors=3)
INFO  | Run abc123: step 2 done status=completed message='Clicked #login-button'
...
INFO  | Run abc123: finished with status=completed
```

**Key healthy signals:**
- `perception GROUNDED` appearing on click/type/select steps → perception found the element
- `status=completed` on every step
- No `WARN` or `ERROR` lines in the executor logger
- `finished with status=completed` at the end

---

## 3. Critical Log Patterns — What They Mean

### Pattern: Step Failure and HITL Escalation

```
ERROR | Run abc123 step 3 (type=click): pre-action health check BLOCKED [error_page] Page appears to be an error page (status=404 detected in text)
WARNING | Run abc123 step 3/5 type=click selector='#submit': selector fallback: ALL candidates FAILED. step_type=click raw_selector='#submit' domain=example.com total_attempts=4 ...
INFO  | Run abc123: step 3 done status=waiting_for_input message='Element not found after 4 attempts'
```
**Meaning:** The agent is blocked. Browser is kept open. User needs to act in the UI within 120 seconds (the `_SELECTOR_INPUT_TIMEOUT_SECONDS` value).

---

### Pattern: HITL Timeout (User Didn't Act in Time)

```
INFO  | Run abc123: selector input timeout scheduled (120s)
INFO  | Run abc123: selector input timeout FIRED — no selector received, closing run
```
**Meaning:** User didn't submit a selector or click Resume in time. Run failed. This is expected behavior, not a system error.

---

### Pattern: Perception Finding the Wrong Element

```
INFO  | perception_probe ... → result=confidence=medium score=61
WARNING | Fast path intent gate REJECTED (step_type=click selector='button' intent='Submit' reason='tag_mismatch_for_type') — escalating to slow path
```
**Meaning:** Perception found an element but wasn't confident. The intent gate rejected it. Fell through to slow path. This is the system working correctly.

---

### Pattern: LLM Selector Repair

```
INFO  | tekno.phantom.brain | POST /v1/selector-suggestions → 200 (1.2s)
INFO  | tekno.phantom.executor | Selector fallback: LLM suggested 3 candidates for step_type=click
```
**Meaning:** Automated recovery was tried. Check if the LLM suggestions actually worked by looking at the next line (`candidate FAILED` vs `step done status=completed`).

---

### Pattern: Recipe Replay

```
INFO  | tekno.phantom.replayer | Replaying recipe for domain=app.example.com step_type=click element_key=submit_button (3 actions)
INFO  | tekno.phantom.replayer | Recipe action 1/3: fill_field selector=#email value=...
INFO  | tekno.phantom.replayer | Recipe replay complete — run resumed
```
**Meaning:** A saved HITL recovery is being replayed automatically. This is the learning system working.

---

### Pattern: Plan Truncation (OpenAI Token Limit)

```
WARNING | tekno.phantom.plan_normalizer | Plan has 12 steps but task has 18 numbered instructions — possible truncation
WARNING | tekno.phantom.api | LLM returned partial JSON — using fallback plan
```
**Meaning:** OpenAI's 1400-token limit was hit. Complex tasks get truncated plans. Switch to Anthropic (`LLM_MODE=anthropic`) for tasks with 15+ steps.

---

### Pattern: Brain Timeout

```
ERROR | tekno.phantom.brain | POST /v1/plan timeout after 10s (BRAIN_TIMEOUT_SECONDS=10)
WARNING | tekno.phantom.api | Brain unreachable — using fallback plan (wait + verify_text)
```
**Meaning:** Brain service is down or overloaded. The fallback plan will almost certainly fail. Check `docker compose logs brain`.

---

### Pattern: Authentication Rate Limit

```
WARNING | tekno.phantom.auth.service | Rate limit hit for IP 192.168.1.1 (10/10 attempts in 900s window)
```
**Meaning:** Someone (or a script) is hammering the login endpoint. This is not a system error — it's the rate limiter working.

---

## 4. What to Look at When a Run Fails

**Step 1 — Find the run ID** (from the UI or from `GET /api/runs`)

**Step 2 — Check the step trace files:**
```
artifacts/<run_id>/step-000.log     ← step 1 trace (JSON)
artifacts/<run_id>/step-001.log     ← step 2 trace
artifacts/<run_id>/step-002-failed.png  ← screenshot at moment of failure
artifacts/<run_id>/report.html      ← full HTML run report
artifacts/<run_id>/summary.txt      ← one-sentence LLM summary
```

**Step 3 — Read the step trace JSON** — the most information-dense artifact:
```json
{
  "step_type": "click",
  "raw_selector": "#submit",
  "intent": { "element_type": "button", "target_text": "Submit" },
  "perception": {
    "confidence": "medium",
    "score": 61,
    "grounded_selector": "#submit",
    "scored_elements": [...]
  },
  "page_health": { "status": "ok", "issues": [] },
  "attempts": [
    { "selector": "#submit", "result": "failed", "error": "Timeout 15000ms" }
  ]
}
```

**Step 4 — Check the drag debug log** (for drag failures):
```
data/drag_debug.jsonl
```
Each line is one drag attempt: strategy used, source/target coords, success/fail, time elapsed.

---

## 5. What a Healthy System Looks Like in Production

**Normal baseline (no problems):**
- Log level `INFO` produces ~20–50 log lines per run
- Every run finishes within `steps × step_timeout_seconds` (5 steps × 60s = 5 min max)
- Brain calls resolve in 2–8 seconds (Anthropic) or 1–4 seconds (OpenAI)
- Perception logs `confidence=high` or `confidence=unique` on most steps
- `status=completed` on > 85% of steps across all runs

**Warning signals (investigate):**
- More than 20% of steps reaching `waiting_for_input` — perception coverage gap, probably shadow DOM or unlabeled elements
- Brain calls timing out repeatedly — check brain container resources or API key quota
- `confidence=ambiguous` appearing on > 30% of steps — the page structure has changed or the task descriptions are vague
- Drag debug log showing strategy 3 (`mouse_event`) firing consistently — target app uses `event.isTrusted` check, drag is not automatable
- Multiple runs stuck at `waiting_for_input` simultaneously — HITL sessions accumulating, nobody is clearing them

**System failure signals (immediate action needed):**
- `Run failed unexpectedly` with a Python exception traceback — unhandled bug in executor
- Brain service returning 500 repeatedly — LLM provider issue or brain crash
- `AUTH_JWT_SECRET must be set` on startup — deployment misconfiguration
- No log files in `backend/logs/` — logging config not initialized (startup failure)

---

## 6. Log Filtering Commands

**Tail live logs in Docker:**
```bash
docker compose logs -f backend
docker compose logs -f brain
```

**Filter to executor only (most useful for debugging a specific run):**
```bash
grep "tekno.phantom.executor" backend/logs/2026-06-08.log | grep "Run abc123"
```

**Find all HITL escalations today:**
```bash
grep "waiting_for_input" backend/logs/2026-06-08.log
```

**Find all perception confidence results:**
```bash
grep "perception_probe" backend/logs/2026-06-08.log
```

**Find all failed steps:**
```bash
grep "status=failed\|ALL candidates FAILED" backend/logs/2026-06-08.log
```

**Find slow brain calls (> 5s):**
```bash
grep "tekno.phantom.brain" backend/logs/2026-06-08.log | grep -E "\b[5-9]\.[0-9]s|\b[0-9]{2}\.[0-9]s"
```

---

## 7. What Is Not Currently Monitored (Gaps)

These health signals have no automated alerting or structured output. You'd only notice them by reading logs manually:

| Gap | Risk |
|---|---|
| No metric for HITL session accumulation rate | Could silently grow if nobody is clearing sessions |
| No alert when brain call latency exceeds threshold | Slow LLM degrades UX without any notification |
| No metric for perception confidence distribution | Hard to detect when a UI change breaks element detection |
| No alert when `data/drag_debug.jsonl` grows too large | File grows unbounded; no rotation |
| No health check on `data/*.sqlite3` sizes | Databases grow indefinitely; no cleanup |
| Recipe store has no TTL | Old recipes accumulate for apps you no longer test |
