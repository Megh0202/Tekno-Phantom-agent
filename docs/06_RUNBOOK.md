# Document 6: Runbook — What To Do When Things Break

> **Purpose:** Step-by-step answers for every common failure scenario.  
> When something goes wrong in production or development, open this document first.  
> Each scenario includes: how to confirm it, immediate fix, and root cause.

---

## Quick Diagnostic Checklist

Before investigating a specific scenario, run through this list:

```
1. Is the brain service running?
   curl http://localhost:8090/health
   Expected: {"status":"ok","mode":"cloud","model":"gpt-4.1-mini"}

2. Is the backend running?
   curl http://localhost:8080/health
   Expected: {"status":"ok","llm":{"status":"ok"},"browser_mode":"playwright",...}

3. Is the browser operational?
   Check backend logs: backend-server.out.log
   Look for: "Playwright browser started" or "BrowserMCPClient initialized"

4. Is the database accessible?
   Check data/ directory exists and contains: run_store.sqlite3, auth.sqlite3
   Check backend logs for SQLite errors

5. Are there stuck runs?
   curl http://localhost:8080/api/runs | python -m json.tool
   Look for runs with status="running" that haven't updated in >5 minutes
```

---

## Scenario 1: Brain Service Is Down or Unreachable

### Symptoms
- `POST /api/plan` returns HTTP 502 with `"Brain plan generation failed"`
- `GET /health` shows `"llm": {"status": "degraded"}`
- Runs fail at first step with `"Brain service unreachable"`

### How to Confirm
```bash
curl http://localhost:8090/health
# If this times out or returns connection refused: brain is down
```

### Immediate Fix

**Option A — Restart the brain service:**
```bash
# If running locally:
cd brain
.venv\Scripts\activate      # Windows
uvicorn app.main:app --host 0.0.0.0 --port 8090

# If running in Docker:
docker-compose restart brain
```

**Option B — Check if API key is missing (most common cause of "degraded" status):**
```bash
# Check brain/.env or root .env
grep ANTHROPIC_API_KEY .env
grep OPENAI_API_KEY .env
# Empty value = brain starts but all LLM calls return fallback plan
```

### Root Cause Options
- Brain service process crashed (check `brain/brain-server.err.log`)
- `BRAIN_BASE_URL` in backend `.env` points to wrong host/port
- LLM API key is missing or expired (brain reports `"status": "degraded"`)
- Docker network issue (containers can't reach each other)

### After Fix
- Verify: `curl http://localhost:8090/health` returns `"status":"ok"`
- Re-run any failed runs from the UI — they are not automatically retried

---

## Scenario 2: Run Stuck in `waiting_for_input` Indefinitely

### Symptoms
- Run status shows `waiting_for_input` and never moves
- User submitted a selector or clicked "Resume" but nothing happened
- Run timeout (120 seconds) should have triggered but didn't

### How to Confirm
```bash
curl http://localhost:8080/api/runs/{run_id}
# Look for: "status": "waiting_for_input", check "updated_at" timestamp
```

### Immediate Fix

**Option A — Force-cancel the stuck run:**
```bash
curl -X POST http://localhost:8080/api/runs/{run_id}/cancel
```

**Option B — If the run is important, manually trigger resume via API:**
```bash
# If waiting for selector:
curl -X POST http://localhost:8080/api/runs/{run_id}/steps/{step_id}/selector \
  -H "Content-Type: application/json" \
  -d '{"selector": "#your-selector-here"}'

# If waiting for HITL recovery confirm:
curl -X POST http://localhost:8080/api/runs/{run_id}/steps/{step_id}/recovery-confirm
```

### Root Cause Options
- The 120-second timeout task was cancelled (backend restart while run was paused)
- The execution lock is held by a crashed coroutine — backend restart required
- The UI submitted the selector but the HTTP call failed silently

### Prevention
- Set `SELECTOR_HELP_MODE=fail` in `.env` if you don't need HITL and want stuck runs to fail immediately instead of waiting
- Reduce `_SELECTOR_INPUT_TIMEOUT_SECONDS` in `executor.py` from 120 to a shorter value

---

## Scenario 3: All Runs Failing — "Element Not Found" on Every Step

### Symptoms
- Every run fails at step 1 or step 2 with `"Element not found"` or `"Timeout waiting for selector"`
- This was working before with the same test cases
- Not specific to one test case — all runs affected

### How to Confirm
Look at `artifacts/<any_recent_run_id>/step-000.log`. If it shows perception attempting to match elements but finding zero candidates, the page snapshot is empty.

### Immediate Fix

**Step 1 — Check if the browser is actually launching:**
```bash
# Backend logs:
type backend\backend-server.out.log | findstr /i "playwright browser"
```

**Step 2 — Check if the target app is reachable:**
```bash
curl -I https://your-target-app.com
# If this returns connection refused or 502: app is down, not your agent
```

**Step 3 — Check if Playwright is installed:**
```bash
cd backend
.venv\Scripts\activate
python -m playwright install chromium
```

**Step 4 — Test browser manually:**
```python
# Run this in the backend venv:
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("https://example.com")
    print(page.title())
    browser.close()
```

### Root Cause Options
- Playwright Chromium binary is missing or corrupted (`playwright install chromium`)
- `PLAYWRIGHT_HEADLESS=true` but Xvfb is not running (Linux only)
- Target app changed its DOM structure significantly — selector memory is stale
- Browser process is crashing silently due to memory constraints

---

## Scenario 4: Authentication Broken — Cannot Log In

### Symptoms
- Login page shows "Invalid credentials" for previously working credentials
- All API calls return HTTP 401 or 403
- `GET /auth/me` returns 401 even with a valid cookie

### How to Confirm
```bash
# Try a fresh login:
curl -X POST http://localhost:8080/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"yourpassword"}'
```

### Immediate Fix

**Option A — Check if auth database exists:**
```bash
dir data\  # Should show auth.sqlite3
```

**Option B — Check if JWT secret is set:**
```bash
grep AUTH_JWT_SECRET .env
# If empty or placeholder ("change-this-secret"), auth will fail on startup
```

**Option C — Reset admin password via bootstrap:**
```bash
# In .env, set these and restart:
AUTH_BOOTSTRAP_ADMIN_EMAIL=admin@example.com
AUTH_BOOTSTRAP_ADMIN_PASSWORD=newpassword123
# Backend will reset the admin password on startup if this is set
```

**Option D — Check token expiry settings:**
```bash
# Access tokens expire in 15 minutes by default
# If the frontend is caching a stale token, clearing cookies fixes it
# Browser Dev Tools → Application → Cookies → delete "tekno_phantom_access"
```

### Root Cause Options
- `AUTH_JWT_SECRET` changed between restarts — all existing tokens are now invalid
- `AUTH_ENABLED=false` → `AUTH_ENABLED=true` upgrade without migrating users
- Token expiry is too short for long-running tasks (default: 15 min access, 7 day refresh)
- Database file permissions issue (backend can't write to `data/auth.sqlite3`)

---

## Scenario 5: SQLite Database Locked or Corrupted

### Symptoms
- Backend logs show `sqlite3.OperationalError: database is locked`
- Run state not persisting between requests
- Backend startup fails with database error

### How to Confirm
```bash
# Check backend logs for SQLite errors:
type backend\backend-server.out.log | findstr /i "sqlite"

# Try opening the database manually:
sqlite3 data\run_store.sqlite3 "SELECT count(*) FROM runs;"
```

### Immediate Fix

**Option A — Locked database (another process has it open):**
```bash
# On Windows, find what has the file open:
# Use Process Explorer or Resource Monitor to find which PID holds the .sqlite3 file
# Kill that process, then restart backend
```

**Option B — Corrupted database:**
```bash
# Check integrity:
sqlite3 data\run_store.sqlite3 "PRAGMA integrity_check;"
# If result is not "ok", the database is corrupted

# Option: restore from backup (if you have one)
# Option: start fresh (data loss):
del data\run_store.sqlite3
# Backend will create a new empty database on next startup
```

**Option C — Disk full:**
```bash
# Check available disk space
wmic logicaldisk get caption,freespace,size
```

### Prevention
- Run periodic SQLite backups: `sqlite3 data/run_store.sqlite3 ".backup data/run_store.backup.sqlite3"`
- Add `WAL mode` to reduce lock contention: `PRAGMA journal_mode=WAL;` (run once on each database)

---

## Scenario 6: VNC Viewer Not Starting

### Symptoms
- `run.viewer_url` is set but the iframe shows nothing or "connection refused"
- `run.viewer_status` stays as `"starting"` and never reaches `"ready"`

### How to Confirm
```bash
# Check if Xvfb is running:
ps aux | grep Xvfb

# Check if x11vnc is running:
ps aux | grep x11vnc

# Check viewer startup logs in backend:
grep -i "viewer\|xvfb\|vnc" backend-server.out.log
```

### Immediate Fix

**This feature only works in Docker on Linux.**

For local Windows development:
```bash
# Set in .env:
BROWSER_VIEWER_ENABLED=false
PLAYWRIGHT_HEADLESS=true
# The browser still runs; you just can't watch it live
```

For Docker/Linux deployment:
```bash
# Confirm Xvfb and x11vnc are installed in the container:
docker exec backend which Xvfb
docker exec backend which x11vnc

# If missing, check the backend Dockerfile has these packages installed
# Restart the container after any Dockerfile changes:
docker-compose build backend
docker-compose up -d backend
```

### Root Cause Options
- Running on Windows — viewer requires Linux
- `BROWSER_VIEWER_ENABLED=false` (default) — explicitly enable it
- Display port conflict — two runs trying to use the same Xvfb display number
- NoVNC static files not found at `/usr/share/novnc` (the hardcoded path in `config.py`)

---

## Scenario 7: Plan Generation Returns Empty or Wrong Steps

### Symptoms
- `POST /api/plan` returns a plan with 0 or 1 steps
- Steps don't match what the task description said
- Plan contains steps for the wrong app (e.g., example.com when a real URL was given)

### How to Confirm
```bash
# Check plan trace in artifacts:
dir artifacts\plan-*
type artifacts\plan-TIMESTAMP\plan-trace.json
# Look at "structured_attempt", "normalized_steps", "validation"
```

### Immediate Fix

**Option A — LLM returned fallback plan (check trace):**
```bash
# In plan-trace.json, look for "raw_llm_response"
# If it's null or empty: LLM call failed entirely — check brain service logs
# If it's non-empty but steps=0: LLM returned valid JSON but no usable steps
```

**Option B — Task description too vague:**
- The structured parser needs clear action verbs: "Go to X, click Y, type Z in field W"
- Rephrase the task as a numbered list: "1. Go to... 2. Click... 3. Type..."

**Option C — Token limit truncated the plan (OpenAI):**
- OpenAI plan prompt has `max_output_tokens=1400`
- For tasks >15 steps, the JSON gets cut off mid-plan, causing JSON parse failure
- Fix: Switch to Anthropic (`LLM_MODE=anthropic` in `brain/.env`) — 8192 token limit

**Option D — Selector profile injecting wrong constraints:**
- If a `selector_profile` was included in the request and it has wrong selectors, the LLM may produce steps that only work for that profile
- Try regenerating the plan without a selector profile

### Root Cause Options
- Brain service using fallback plan (`fallback_plan()`) because LLM returned unparseable output
- OpenAI `max_output_tokens=1400` hit on complex tasks (upgrade to Anthropic or `gpt-4o`)
- Structured parser matched the task and returned incorrect steps (check `structured_attempt` in plan trace)
- Task contains URL from a different app than intended

---

## Scenario 8: Docker Containers Won't Start

### Symptoms
- `docker-compose up` fails or containers immediately exit
- Backend or brain container shows exit code 1

### How to Confirm
```bash
docker-compose up 2>&1 | head -50
docker-compose logs backend | tail -30
docker-compose logs brain | tail -30
```

### Common Fixes

**Missing `.env` file:**
```bash
copy .env.example .env
# Then set required values: BRAIN_BASE_URL, AUTH_JWT_SECRET (if AUTH_ENABLED=true)
```

**Missing `data/` directory:**
```bash
mkdir data
# SQLite databases are created at startup but the directory must exist
```

**Port already in use:**
```bash
# Check what's using port 8080 / 8090 / 3000:
netstat -ano | findstr :8080
# Kill the process or change port in docker-compose.yml
```

**`AUTH_JWT_SECRET` is placeholder:**
```bash
# In .env:
AUTH_JWT_SECRET=change-this-secret   ← this causes startup failure when AUTH_ENABLED=true
# Fix: set a real random string
AUTH_JWT_SECRET=your-random-64-char-string-here
```

**Frontend build fails (Next.js):**
```bash
docker-compose logs frontend | grep -i error
# Usually: missing NEXT_PUBLIC_API_BASE_URL or package install failure
# Fix: ensure .env has NEXT_PUBLIC_API_BASE_URL set (can be empty for same-origin)
```

---

## Scenario 9: Selector Memory Growing Too Large / Slow Lookups

### Symptoms
- Step execution latency increasing over time
- `data/selector_memory.sqlite3` file is very large (>100MB)
- Logs show slow candidate lookups

### How to Confirm
```bash
sqlite3 data\selector_memory.sqlite3 "SELECT count(*) FROM selector_memory;"
sqlite3 data\selector_memory.sqlite3 "SELECT count(*) FROM element_signatures;"
```

### Fix

**Prune old/stale selectors:**
```bash
# Remove entries older than 30 days:
sqlite3 data\selector_memory.sqlite3 "DELETE FROM selector_memory WHERE last_seen < datetime('now', '-30 days');"
sqlite3 data\selector_memory.sqlite3 "VACUUM;"
```

**Limit candidates per key (already configurable):**
```bash
# In .env:
SELECTOR_MEMORY_MAX_CANDIDATES=5   # already the default; reduce to 3 if needed
```

---

## Scenario 10: Step Fails With "Timeout" But Element Is Visible

### Symptoms
- A click/type step times out even though the target element is clearly visible on the page
- The `step-XXX.log` shows perception found 0 matches
- The element exists but has no ARIA label or visible text

### Diagnosis
This is a **perception coverage failure** — the most common remaining class of failures.

Check `step-XXX.log` for lines like:
```
perception: 0 elements in index
perception: match=None confidence=none
```

### Fix Options

**Option A — Add a selector profile entry:**
1. Open the test case in the UI
2. Go to selector profile
3. Add a key (e.g., `"icon_button"`) with the correct CSS selector for this element
4. Reference it in the step: `{{selector.icon_button}}`

**Option B — Provide a `target` with ARIA scope:**
Edit the step to include a semantic target with scope:
```json
{
  "type": "click",
  "target": {
    "semantic_name": "settings icon",
    "expected_role": "button",
    "scope": "top navigation"
  }
}
```

**Option C — Use HITL to record and replay:**
1. Let the run pause at the failing step
2. Click "Take over" in the UI
3. Perform the click manually
4. Click "Resume"
5. The interaction is saved and will be replayed automatically on the next run

### Root Cause
The element is an icon button with no text, no aria-label, and no placeholder. The perception engine has no signal to score it higher than zero. This is a known perception gap — see `03_PROBLEMS_LESSONS_DEBT.md` Problem #1.

---

## Log File Reference

| File | What It Contains | When to Check |
|---|---|---|
| `backend/backend-server.out.log` | All backend INFO+ logs: run start/end, step execution, errors | Any backend failure |
| `backend/backend-server.err.log` | Backend stderr: Python tracebacks, startup errors | Backend crashes / startup failures |
| `brain/brain-server.out.log` | Brain service logs: LLM calls, parse errors, fallback triggers | Plan generation issues |
| `brain/brain-server.err.log` | Brain stderr / tracebacks | Brain service crashes |
| `artifacts/<run_id>/step-XXX.log` | Per-step execution trace: which execution path ran, perception scores, selector attempts | Understanding why a specific step failed |
| `artifacts/<run_id>/report.html` | Human-readable run report | Sharing results with non-technical stakeholders |
| `artifacts/<run_id>/step-XXX-failed.png` | Screenshot at the moment of failure | Visual diagnosis of what was on screen |
| `data/drag_debug.jsonl` | All drag attempt details: strategy, coordinates, success/fail per strategy | Drag failures |
| `artifacts/plan-<timestamp>/plan-trace.json` | Full planning pipeline trace | Understanding why a plan looks wrong |

---

## Emergency Procedures

### Full Reset (development only — data loss)

```bash
# Stop all services
docker-compose down

# Delete all run data and artifacts (DESTRUCTIVE):
rmdir /s /q artifacts
rmdir /s /q data
mkdir artifacts
mkdir data

# Restart
docker-compose up
```

### Restart Backend Without Losing Run Data

```bash
# Safe restart — SQLite data persists
docker-compose restart backend

# Or locally:
# Find and kill the uvicorn process, then restart
taskkill /f /im python.exe   # Windows — be careful: kills all Python processes
cd backend && .venv\Scripts\uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### Check System Health in One Command

```bash
curl -s http://localhost:8080/health | python -m json.tool
```

Expected healthy output:
```json
{
  "status": "ok",
  "llm": { "status": "ok", "mode": "anthropic", "model": "claude-sonnet-4-6" },
  "browser_mode": "playwright",
  "filesystem_mode": "local",
  "run_store_backend": "sqlite",
  "max_steps_per_run": 300
}
```

Any field showing `"status": "degraded"` indicates a problem in that subsystem.
