# Document 8: Configuration Interaction Map

> **Purpose:** Every env var in `config.py`, what it does, which other settings it depends on, and recommended values for production vs. development.  
> **Source of truth:** `backend/app/config.py` (147 lines). All vars are read from `.env` or environment. The brain service reads its own separate `brain/.env`.

---

## How Configuration Works in This Project

All settings live in a `.env` file at the project root. The backend reads them once at startup using Pydantic — if a required field is missing or wrong, the app refuses to start with a clear error message. Changing `.env` while the server is running does nothing; you must restart.

The brain service is completely independent. It has its own `brain/.env` and its own config. The backend does not know which LLM the brain uses — it just calls the brain over HTTP and expects a response. Swapping from OpenAI to Anthropic requires only changing `brain/.env` and restarting the brain container.

---

## 1. Dependency Chains (Settings That Only Work When Others Are Set)

These are the non-obvious interactions. Misconfiguring one will silently disable the other.

### Chain A: Browser Viewer (VNC live browser stream)

**What this is:** When a run is executing, the user can watch the browser in real time through a VNC stream embedded in the UI. This is how HITL works — the user sees exactly what the agent sees and can take over the browser directly. Each run gets its own isolated virtual display (Xvfb), VNC server (x11vnc), and Chromium window — all inside the Docker container.

**Why it only works on Linux/Docker:** The virtual display system (Xvfb) is Linux-only. On a Windows or macOS development machine, Playwright can open a visible browser window locally, but there's no VNC layer to stream it through the UI. The path `/usr/share/novnc` (where the VNC web client lives) is also a Linux path hardcoded in the config.

```
BROWSER_VIEWER_ENABLED=true
    └── requires PLAYWRIGHT_HEADLESS=false
            └── requires BROWSER_MODE=playwright (not mock, not mcp)
                    └── requires running inside Docker on Linux
                            (viewer_static_root=/usr/share/novnc — Linux path hardcoded)
```
**If any dependency is missing:** `ViewerSessionManager.enabled` returns `False`. Runs get no `viewer_url`. No error is raised — the feature silently does nothing.

**Note from code (`viewer_session.py:53`):**
```python
def enabled(self) -> bool:
    return bool(self._settings.browser_viewer_enabled) and not bool(self._settings.playwright_headless)
```

### Chain B: HITL Selector Recovery

**What this is:** When the agent cannot find an element after exhausting all automated options, instead of immediately failing the run, it can pause and wait for a human. The human can either type in a CSS selector they know works, or simply perform the action themselves in the live browser. `SELECTOR_HELP_MODE` is the master switch that decides which behavior happens.

**Why `SELECTOR_HELP_MODE=fail` is recommended in development:** In development, you want failures to surface fast so you can debug them. You don't want the run sitting paused, waiting for human input, because you might not be watching. Set it to `fail` during development, `pause` in production.

```
SELECTOR_RECOVERY_ENABLED=true
    └── requires SELECTOR_HELP_MODE=pause     (if "fail", step fails instead of pausing)
            └── SELECTOR_RECOVERY_ATTEMPTS=N  controls how many times the step is retried
                    after the user submits a selector before giving up
```
**If `SELECTOR_HELP_MODE=fail`:** The run never reaches `waiting_for_input`. Steps just fail immediately. HITL cannot trigger. `SELECTOR_RECOVERY_ENABLED` is then meaningless.

### Chain C: LLM Selector Repair (brain fallback)

**What this is:** When a step fails, before giving up or asking a human, the executor can call the brain service and say "here's the failed element and the current page DOM — suggest better CSS selectors." The brain passes this to the LLM, which generates alternative selectors based on what it sees. This is entirely automated — the human doesn't know it happened unless they read the logs.

**Why you'd disable this in development:** LLM calls take 2–8 seconds. If you're debugging failures and triggering them repeatedly, you don't want to wait for an LLM call on every failure. Set `SELECTOR_LLM_RECOVERY_ENABLED=false` during dev.

```
SELECTOR_LLM_RECOVERY_ENABLED=true
    └── requires SELECTOR_RECOVERY_ENABLED=true   (LLM repair is a sub-layer of recovery)
            └── requires brain service reachable at BRAIN_BASE_URL
                    └── requires LLM_MODE set correctly in brain service
```
**If brain is down:** `selector_llm_recovery_enabled` is irrelevant. The executor catches the brain timeout and skips LLM suggestions.

### Chain D: Auth Stack

**What this is:** The auth system protects the API with JWT tokens stored in HTTP-only cookies. A JWT (JSON Web Token) is a signed token — the server creates it with a secret key, and the browser sends it back with every request. The server verifies the signature to confirm the token wasn't tampered with. If someone steals your `AUTH_JWT_SECRET`, they can forge tokens for any user.

**Why the startup guard exists:** The `config.py:model_post_init` method checks the secret on startup and refuses to start if it's empty or a placeholder. This is intentional — it is far better to crash at startup with a clear message than to silently run a production system with a known-compromised secret.

**Bootstrap admin:** The first time the system starts with auth enabled, it creates one admin user from `AUTH_BOOTSTRAP_ADMIN_EMAIL` and `AUTH_BOOTSTRAP_ADMIN_PASSWORD`. On every subsequent startup, these are ignored (the user already exists). If you forget to set these before first startup, you'll have no admin user and no way to log in — you'd need to manually insert a user into the SQLite auth database.

```
AUTH_ENABLED=true
    └── requires AUTH_JWT_SECRET set (non-empty, non-placeholder)
    └── requires AUTH_COOKIE_SECURE=true IF AUTH_COOKIE_SAMESITE=none
    └── AUTH_BOOTSTRAP_ADMIN_EMAIL + AUTH_BOOTSTRAP_ADMIN_PASSWORD
            → creates admin user on first startup; ignored on subsequent starts
```
**If `AUTH_JWT_SECRET` is missing or a placeholder:** App fails to start with `ValueError`. This is enforced in `config.py:model_post_init`.

### Chain E: Recovery Recipe (learned HITL replay)

**What this is:** When a human performs an action manually during a HITL session, the system records every click, type, and select they made, normalizes it into a "recipe", and saves it to SQLite. The next time the exact same failure occurs on the same domain, the system automatically replays that recipe — no human needed. This is how HITL sessions become automated over time.

**Why you'd disable this in development:** During development, you might intentionally trigger failures to debug something. If recipe saving is on, the first failure saves a recipe, and subsequent runs replay it — making it look like the system recovered when it actually just used your debug-session actions as a recipe.

```
RECOVERY_RECIPE_ENABLED=true
    └── requires SELECTOR_RECOVERY_ENABLED=true   (recipes are only used during recovery flow)
            └── RECOVERY_RECIPE_BACKEND=sqlite     → writes to RECOVERY_RECIPE_DB_PATH
```
**If `RECOVERY_RECIPE_ENABLED=false`:** Human interactions are not saved. Each HITL session is thrown away — the system never learns.

### Chain F: Fast Path Execution

**What this is:** When a step has a complete semantic target (the LLM specified exactly what element to interact with, including role and accessible name), the executor can take a shortcut — it goes directly from perception to browser action without running the full selector fallback cascade. This makes successful steps significantly faster.

**What fast path is NOT:** It doesn't skip perception. Perception still runs on the fast path. What it skips is the slow fallback cascade (memory lookup, LLM repair, multiple retries). If the fast path action times out or the intent gate rejects the selector, the system automatically falls back to the slow path.

```
EXECUTION_FAST_PATH_ENABLED=true
    └── EXECUTION_FAST_PATH_ACTION_TIMEOUT_SECONDS   (default 4s — max time for fast-path action)
    └── EXECUTION_FAST_PATH_SELECTOR_TIMEOUT_MS       (default 2000ms — element probe timeout)
```
If fast path times out: escalates to slow path automatically. If `EXECUTION_FAST_PATH_ENABLED=false`, all steps use the full slow path (perception → fallback cascade always runs).

---

## 2. Full Variable Reference

> **Reading this section:** Each table shows the variable name, its default value, the recommended production value, the recommended development value, and notes explaining what it actually does and why. "Prod" and "Dev" columns are recommendations — they are not enforced by the code.

### Networking & Services

**What this section is:** How the backend knows where to find the brain service, how long to wait for it, and what external origins are allowed to make API requests.

**Why `BRAIN_BASE_URL` is different in Docker:** Inside Docker, services communicate by their service name (`brain`, `frontend`, `backend`) not by `localhost`. The docker-compose.yml sets `BRAIN_BASE_URL=http://brain:5000` automatically when the backend container starts.

**What `CORS_ORIGINS` does:** Browsers block JavaScript from making requests to a different domain unless the server explicitly allows it. This variable tells the backend which frontend origins are allowed. If your frontend is at `https://app.example.com`, that value must be in `CORS_ORIGINS` or all API calls will be blocked by the browser.

| Variable | Default | Prod | Dev | Notes |
|---|---|---|---|---|
| `BRAIN_BASE_URL` | `http://localhost:8090` | `http://brain:5000` | `http://localhost:8090` | Docker: use service name `brain:5000` |
| `BRAIN_API_KEY` | `""` | set a shared secret | leave empty | Optional auth between backend and brain |
| `BRAIN_TIMEOUT_SECONDS` | `10` | `15` | `10` | Brain LLM calls can take 8–12s on complex plans |
| `BACKEND_PORT` | `8080` | `8000` (Docker) | `8080` | Docker compose overrides to `8000` |
| `CORS_ORIGINS` | `http://localhost:3000` | your frontend domain | `http://localhost:3000` | Comma-separated |
| `ADMIN_API_TOKEN` | `""` | set a strong token | optional | Token for internal admin endpoints |

### Auth

| Variable | Default | Prod | Dev | Notes |
|---|---|---|---|---|
| `AUTH_ENABLED` | `false` | `true` | `false` | **Must be true in production** |
| `AUTH_JWT_SECRET` | `""` | 64+ char random string | any non-empty string | Fails to start if missing when auth enabled |
| `AUTH_ACCESS_TOKEN_EXPIRES_MINUTES` | `15` | `15` | `60` | Longer in dev to avoid constant re-login |
| `AUTH_REFRESH_TOKEN_EXPIRES_DAYS` | `7` | `7` | `30` | |
| `AUTH_COOKIE_SECURE` | `false` | `true` | `false` | Must be true if HTTPS |
| `AUTH_COOKIE_SAMESITE` | `lax` | `lax` | `lax` | Use `strict` for no cross-site. `none` requires HTTPS |
| `AUTH_COOKIE_DOMAIN` | `""` | `.yourdomain.com` | `""` | Needed for subdomain cookie sharing |
| `AUTH_RATE_LIMIT_IP_MAX_ATTEMPTS` | `10` | `10` | `100` | Higher in dev to avoid lockouts |

### Execution Limits

| Variable | Default | Prod | Dev | Notes |
|---|---|---|---|---|
| `MAX_STEPS_PER_RUN` | `300` | `50` | `20` | `.env.example` uses 20; 300 is the code default |
| `STEP_TIMEOUT_SECONDS` | `60` | `60` | `90` | Time each step has to complete |
| `DRAG_STEP_TIMEOUT_SECONDS` | `120` | `120` | `180` | Drag is slower; separate budget |
| `STEP_FAILURE_MODE` | `continue` | `continue` | `continue` | `stop` halts run on first failure |

### Browser

| Variable | Default | Prod | Dev | Notes |
|---|---|---|---|---|
| `BROWSER_MODE` | `playwright` | `playwright` | `mock` | `mock` for tests, `playwright` for real runs |
| `PLAYWRIGHT_BROWSER` | `chromium` | `chromium` | `chromium` | `firefox`/`webkit` untested |
| `PLAYWRIGHT_HEADLESS` | `true` | `true` | `false` | Set `false` only to enable viewer (Linux/Docker only) |
| `PLAYWRIGHT_DEFAULT_TIMEOUT_MS` | `15000` | `15000` | `30000` | Higher in dev (docker-compose.dev.yml uses 30000) |
| `PLAYWRIGHT_SLOW_MO_MS` | `0` | `0` | `500` | Add 500ms between actions for debugging |

### Browser Viewer (VNC)

| Variable | Default | Prod | Dev | Notes |
|---|---|---|---|---|
| `BROWSER_VIEWER_ENABLED` | `false` | `true` | `false` | **Requires PLAYWRIGHT_HEADLESS=false** |
| `VIEWER_DISPLAY_START` | `99` | `99` | n/a | X display number pool start |
| `VIEWER_DISPLAY_END` | `140` | `140` | n/a | Supports up to 42 concurrent run viewers |
| `VIEWER_VNC_PORT_START` | `5900` | `5900` | n/a | VNC port pool |
| `VIEWER_VNC_PORT_END` | `5941` | `5941` | n/a | 42 concurrent viewers max |
| `VIEWER_SCREEN_GEOMETRY` | `1440x960x24` | `1440x960x24` | n/a | Display resolution |
| `VIEWER_STARTUP_TIMEOUT_SECONDS` | `12` | `12` | n/a | How long to wait for Xvfb+VNC to be ready |
| `VIEWER_KEEPALIVE_SECONDS` | `45` | `45` | n/a | How long to keep browser open after HITL pause |

### Drag Execution

**What this section is:** Settings for the three-strategy drag-and-drop system. Drag is the hardest browser action to automate — some apps check whether mouse events are "trusted" (generated by a real user) and reject synthetic events. These settings tune the drag behavior.

**Why `DRAG_USE_FIXED_COORDS=true`:** By default, the system calculates the drop zone by taking the source element's center coordinates and adding an X and Y offset. This is more reliable than trying to locate the drop target by CSS selector, because drag targets often have no stable selector. The offset values (`260`, `180`) were measured for the specific apps that were tested — for a new app, you'd need to measure where the drop zone is relative to the draggable element.

**Why `DRAG_MOUSE_STEPS=24`:** The drag simulation moves the mouse in 24 small steps from source to target (rather than jumping instantly). Some apps track mouse velocity and reject drags that move too fast. 24 steps produces a realistic-looking mouse path. Increasing this makes the drag slower but more realistic.

**Why `DRAG_DEBUG_LOG_ENABLED=true` always:** The `drag_debug.jsonl` file records every drag attempt with full details — which strategy was tried, the coordinates used, whether the element moved after the drop, and how long it took. This is the only observability for drag failures. Keeping it on costs almost nothing (tiny file) but is invaluable when debugging.

| Variable | Default | Prod | Dev | Notes |
|---|---|---|---|---|
| `DRAG_USE_FIXED_COORDS` | `true` | `true` | `true` | Use element center coords, not selector-based |
| `DRAG_TARGET_X_OFFSET` | `260` | app-specific | app-specific | Pixel offset from source center for drop zone |
| `DRAG_TARGET_Y_OFFSET` | `180` | app-specific | app-specific | Tuned for tested apps — may need adjustment |
| `DRAG_RETRY_RADIUS_PX` | `40` | `40` | `40` | Retry radius if first drop coordinate misses |
| `DRAG_VALIDATION_WAIT_MS` | `180` | `180` | `300` | Wait after drop to check if element moved |
| `DRAG_MOUSE_STEPS` | `24` | `24` | `24` | Intermediate steps in drag movement (smoother = 24+) |
| `DRAG_DEBUG_LOG_ENABLED` | `true` | `true` | `true` | Writes `data/drag_debug.jsonl` — very useful |

### Recovery and Memory

**What this section is:** Settings for the selector memory system (which remembers which CSS selectors worked before) and the HITL recovery system.

**Selector memory explained:** Every time a selector successfully clicks/types/selects an element, the system writes that selector to `selector_memory.sqlite3` keyed by `(domain, step_type, element_key)`. Next time the same step runs on the same app, it loads up to `SELECTOR_MEMORY_MAX_CANDIDATES=5` remembered selectors and tries those first — before asking perception or the LLM. This is why the system gets more reliable over time on apps it has already tested.

| Variable | Default | Prod | Dev | Notes |
|---|---|---|---|---|
| `SELECTOR_MEMORY_ENABLED` | `true` | `true` | `true` | |
| `SELECTOR_MEMORY_BACKEND` | `sqlite` | `sqlite` | `sqlite` | `in_memory` for tests |
| `SELECTOR_MEMORY_MAX_CANDIDATES` | `5` | `5` | `5` | Max historical selectors loaded per step |
| `SELECTOR_RECOVERY_ENABLED` | `true` | `true` | `true` | |
| `SELECTOR_HELP_MODE` | `pause` | `pause` | `fail` | `fail` in dev to surface errors fast |
| `SELECTOR_RECOVERY_ATTEMPTS` | `2` | `2` | `1` | |
| `SELECTOR_RECOVERY_DELAY_MS` | `350` | `350` | `350` | Pause between retry attempts |
| `SELECTOR_LLM_RECOVERY_ENABLED` | `true` | `true` | `false` | Disable in dev to avoid LLM calls on every failure |
| `SELECTOR_LLM_MAX_CANDIDATES` | `3` | `3` | `3` | Max LLM-suggested selectors to try |
| `RECOVERY_RECIPE_ENABLED` | `true` | `true` | `false` | Disable in dev if you don't want HITL replay |
| `RECOVERY_RECIPE_BACKEND` | `sqlite` | `sqlite` | `sqlite` | |

### Wait Timings (Advanced)

**What this section is:** The agent injects small waits between browser actions because web pages aren't instant. After clicking "Submit", the page needs a moment to send the form to the server, get a response, and redirect. If the agent moves to the next step immediately, it may try to interact with the old page before the new one has loaded.

**Why there are so many wait values:** Different action types have different timing needs. A login form redirect is slower than clicking a checkbox. A drag-and-drop requires a stabilization wait. A page navigate during recovery needs a much longer wait (10 seconds) because the system might be waiting for a slow app to fully initialize. Each value was tuned from testing specific flows.

**What to change when tests are flaky:** If a test is failing intermittently because the agent is acting too fast, increase `DEFAULT_WAIT_MS` or the specific wait type for that action. If tests are slow, reduce them — but reduce cautiously, because reducing too much causes "element not found" errors on slower machines.

These control automatic waits injected between browser actions. Changing them affects test reliability.

| Variable | Default | When It Fires |
|---|---|---|
| `DEFAULT_WAIT_MS` | `450` | After most actions |
| `AUTO_LOGIN_WAIT_MS` | `500` | After submitting a login form |
| `AUTO_CREATE_CONFIRM_WAIT_MS` | `450` | After clicking Create/Save/Submit buttons |
| `AUTO_DRAG_POST_WAIT_MS` | `120` | After drag drop completes |
| `PLANNER_DEFAULT_WAIT_MS` | `1000` | Default wait step duration when LLM generates a wait step |
| `RECOVERY_LOAD_STATE_WAIT_MS` | `10000` | Wait after navigating to a new page during recovery (10s) |
| `STRUCTURED_SELECTOR_WAIT_MS` | `6000` | Timeout for structured selector resolution |
| `STRUCTURED_OPTIONS_WAIT_MS` | `5000` | Timeout for select `<option>` enumeration |
| `EXECUTION_FAST_PATH_ACTION_TIMEOUT_SECONDS` | `4` | Fast path max action time |
| `EXECUTION_FAST_PATH_SELECTOR_TIMEOUT_MS` | `2000` | Fast path element probe timeout |

---

## 3. Brain Service Settings (Separate `.env`)

**What the brain service is:** A separate FastAPI microservice that owns all LLM communication. The backend never calls an LLM directly — it always calls the brain over HTTP. This design means you can swap the LLM provider (OpenAI → Anthropic → local vLLM) without touching any backend code. Only the brain's `.env` changes.

**Why Anthropic is recommended over OpenAI:** OpenAI's output token limit is 1400 tokens for plans. A complex task with 15+ steps can hit this limit, producing a truncated plan where the last few steps are silently dropped. Anthropic's limit is 8192 tokens — no plan will ever hit this in practice. Anthropic also supports vision-based failure diagnosis (`diagnose_failure()`) which the OpenAI provider does not implement.

**What vLLM is:** An open-source inference engine that runs LLMs locally on your own hardware. If you have a GPU machine, you can run a model like LLaMA 3.1 locally without any API cost. The brain connects to it through the OpenAI-compatible API format. Quality is lower than GPT-4 or Claude for complex planning, but it's free to run.

The brain service (`brain/`) has its own environment, not shared with the backend.

| Variable | Default | Notes |
|---|---|---|
| `LLM_MODE` | `local` | `local` (vLLM), `cloud` (OpenAI), `anthropic` |
| `OPENAI_API_KEY` | `""` | Required when `LLM_MODE=cloud` |
| `OPENAI_MODEL` | `gpt-4.1-mini` | |
| `ANTHROPIC_API_KEY` | `""` | Required when `LLM_MODE=anthropic` |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | |
| `VLLM_BASE_URL` | `http://localhost:8001/v1` | Required when `LLM_MODE=local` |
| `VLLM_MODEL` | `meta-llama/Llama-3.1-8B-Instruct` | Must match what vLLM is serving |
| `BRAIN_API_KEY` | `""` | Must match backend's `BRAIN_API_KEY` if set |

---

## 4. Docker Compose vs. Local Dev Differences

| Setting | Local dev (direct uvicorn) | Docker Compose (prod) | Docker Compose dev override |
|---|---|---|---|
| `BACKEND_PORT` | `8080` | `8000` | `8000` |
| `BRAIN_BASE_URL` | `http://localhost:8090` | `http://brain:5000` | `http://brain:5000` |
| `BROWSER_MODE` | from `.env` | from `.env` | from `.env` |
| `CORS_ORIGINS` | `http://localhost:3000` | your domain | your domain |
| `PLAYWRIGHT_DEFAULT_TIMEOUT_MS` | `15000` | `15000` | `30000` (overridden) |
| `STRUCTURED_SELECTOR_WAIT_MS` | `6000` | `6000` | `10000` (overridden) |
| Volumes | local `data/` and `artifacts/` dirs | Docker named volumes `backend_data`, `backend_artifacts` | same as compose |

**Key Docker difference:** Data is persisted in Docker named volumes. To inspect: `docker volume inspect tekno-phatom-agent_backend_data`. To reset: `docker volume rm tekno-phatom-agent_backend_data` (deletes all run history and learned selectors).

---

## 5. Recommended Production Checklist

```
AUTH_ENABLED=true
AUTH_JWT_SECRET=<64+ random chars — use: python -c "import secrets; print(secrets.token_hex(32))">
AUTH_COOKIE_SECURE=true              # HTTPS only
AUTH_COOKIE_SAMESITE=lax
AUTH_BOOTSTRAP_ADMIN_EMAIL=you@yourdomain.com
AUTH_BOOTSTRAP_ADMIN_PASSWORD=<strong password>

BRAIN_API_KEY=<shared secret>        # set same value in brain .env

LLM_MODE=anthropic                   # or cloud
ANTHROPIC_API_KEY=<key>

BROWSER_MODE=playwright
PLAYWRIGHT_HEADLESS=true
BROWSER_VIEWER_ENABLED=false         # enable only if you want live viewer

SELECTOR_HELP_MODE=pause             # enables HITL
SELECTOR_LLM_RECOVERY_ENABLED=true
RECOVERY_RECIPE_ENABLED=true

LOG_LEVEL=INFO
```

## 6. Recommended Dev / Local Checklist

```
AUTH_ENABLED=false                   # no login friction
BROWSER_MODE=mock                    # for unit tests
BROWSER_MODE=playwright              # for real automation testing
PLAYWRIGHT_HEADLESS=false            # see the browser (Windows dev machine)
PLAYWRIGHT_SLOW_MO_MS=500           # easier to watch

SELECTOR_HELP_MODE=fail              # surface errors fast, no HITL blocking
SELECTOR_LLM_RECOVERY_ENABLED=false # avoid LLM calls on every failure
RECOVERY_RECIPE_ENABLED=false       # don't accumulate recipes during dev

LLM_MODE=anthropic
ANTHROPIC_API_KEY=<key>
LOG_LEVEL=DEBUG
```
