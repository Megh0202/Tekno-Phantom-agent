# Document 10: Production Deployment Runbook

> **Purpose:** How to deploy the system on a real server, configure it securely, keep data safe, and recover from common problems.  
> **This is not the quick-start.** The README covers local development. This document covers what you do when you're putting this on a server that real users will access.

---

## How the System Is Structured (So the Deployment Makes Sense)

Before deploying, it helps to understand what you're actually deploying. The system has three services that run as Docker containers, sitting behind an Nginx reverse proxy:

```
Internet → Nginx (port 80/443)
              ├── /          → Frontend (Next.js, port 3000)
              ├── /api/      → Backend (FastAPI, port 8000)
              ├── /auth/     → Backend (same, auth routes)
              ├── /viewer/   → Backend (WebSocket for VNC viewer)
              └── /brain/    → Brain (FastAPI LLM service, port 5000)
```

**Frontend (Next.js):** The user interface. It talks to the backend over `/api/`. It is a static-ish React app — all env vars are baked in at Docker build time (not at runtime). This matters because `NEXT_PUBLIC_API_BASE_URL` must be set correctly before you run `docker compose build`.

**Backend (FastAPI):** The main API server. It handles run creation, execution, HITL, auth, and artifact storage. It runs Playwright (a real Chromium browser) inside the Docker container. The browser is what actually clicks things on web pages.

**Brain (FastAPI):** The LLM microservice. It receives task descriptions from the backend and returns step plans. It's the only service that ever calls OpenAI or Anthropic. The backend doesn't know or care which LLM the brain uses.

**Nginx:** The entry point for all traffic. It routes requests to the right service based on the URL path. It also handles WebSocket upgrades for the VNC viewer.

---

## Prerequisites

Before starting, you need:

- **A Linux server** — The VNC viewer (for HITL live browser streaming) uses Xvfb and x11vnc, which are Linux-only. The system can be deployed on Windows/macOS but the viewer feature won't work.
- **Docker Engine 24+ and Docker Compose v2+** — Check with `docker --version` and `docker compose version`.
- **A domain name** — For SSL. Pointing the domain at your server's IP address should be done before you start.
- **An LLM API key** — Either Anthropic (`ANTHROPIC_API_KEY`) or OpenAI (`OPENAI_API_KEY`). Anthropic is recommended (better plan quality, no token truncation on complex tasks).
- **The project code** — Clone to `/opt/tekno-phantom` or any preferred path.

---

## 1. First-Time Production Setup

### Step 1: Generate your secrets

You need two random secrets that no one else knows. These are used to sign authentication tokens.

```bash
# JWT secret for signing user sessions
python3 -c "import secrets; print(secrets.token_hex(32))"
# Example output: a3f8b2c1d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1

# Admin API token (optional — for admin-only API endpoints)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Keep these secret. If someone has your JWT secret, they can forge sessions as any user.

---

### Step 2: Create the backend `.env`

```bash
cp .env.example .env
```

Open `.env` and change these values from the example defaults:

```env
# --- Auth (required for production) ---
AUTH_ENABLED=true
AUTH_JWT_SECRET=<your-generated-64-char-secret>
AUTH_COOKIE_SECURE=true        # because you'll have HTTPS
AUTH_BOOTSTRAP_ADMIN_EMAIL=admin@yourdomain.com
AUTH_BOOTSTRAP_ADMIN_PASSWORD=<a-strong-password>

# --- Brain service (Docker service name, not localhost) ---
BRAIN_BASE_URL=http://brain:5000
BRAIN_API_KEY=<shared-secret-same-as-brain-env>   # optional but recommended

# --- Browser ---
BROWSER_MODE=playwright
PLAYWRIGHT_HEADLESS=true
BROWSER_VIEWER_ENABLED=false   # set true if you want HITL live viewer

# --- Recovery ---
SELECTOR_HELP_MODE=pause       # enables HITL
SELECTOR_LLM_RECOVERY_ENABLED=true
RECOVERY_RECIPE_ENABLED=true

# --- CORS (your actual frontend URL) ---
CORS_ORIGINS=https://yourdomain.com

LOG_LEVEL=INFO
```

**Why `BRAIN_BASE_URL=http://brain:5000`?** Inside Docker, containers communicate using the service name defined in `docker-compose.yml`. The brain service is named `brain`, so the backend reaches it at `http://brain:5000`. On your local machine without Docker, you'd use `http://localhost:8090`.

---

### Step 3: Create the brain `.env`

```bash
cp brain/.env.example brain/.env
```

Edit `brain/.env`:

```env
LLM_MODE=anthropic
ANTHROPIC_API_KEY=<your-anthropic-key>
ANTHROPIC_MODEL=claude-sonnet-4-6
BRAIN_API_KEY=<same-shared-secret-as-backend>   # must match backend BRAIN_API_KEY
LOG_LEVEL=INFO
```

---

### Step 4: Set frontend build-time variables

The Next.js frontend bakes certain values into the JavaScript bundle at build time. These are environment variables starting with `NEXT_PUBLIC_`. You must set them before building, not before running.

```bash
# In your shell, before running docker compose build:
export NEXT_PUBLIC_API_BASE_URL=https://yourdomain.com
export NEXT_PUBLIC_ADMIN_API_TOKEN=<your-admin-token>   # optional
export NEXT_PUBLIC_SHOW_ADVANCED_INPUTS=false
```

Or set them in `docker-compose.yml` under `frontend.build.args` — those values are already wired in the `args:` section of the compose file.

---

### Step 5: Build and start

```bash
docker compose up --build -d
```

**What `--build` does:** Builds the Docker images from scratch (installs Python deps, installs Playwright + Chromium, builds Next.js). This takes 5–10 minutes on first run. On subsequent starts without code changes, use `docker compose up -d` (no rebuild needed).

**What `-d` does:** Runs in detached mode (background). Without it, logs print to your terminal and stopping the terminal stops the containers.

---

### Step 6: Verify it started correctly

```bash
# Are all four services running?
docker compose ps

# Does the backend respond?
curl http://localhost/api/health
# Expected: {"status": "ok"}

# Does the brain respond?
curl http://localhost/brain/health
# Expected: {"status": "ok"}

# Any errors in startup logs?
docker compose logs --tail=50 backend
docker compose logs --tail=50 brain
```

**First startup note:** The backend creates the admin user from `AUTH_BOOTSTRAP_ADMIN_EMAIL` on first start. You'll see a log line like `Created bootstrap admin user: admin@yourdomain.com`. After this, those env vars are ignored.

---

## 2. Setting Up HTTPS (SSL)

The system works on HTTP out of the box (port 80), but production deployments must use HTTPS. HTTPS encrypts all traffic between the user's browser and your server — without it, session tokens are sent in plaintext and can be stolen.

### How TLS termination works here

Nginx handles SSL. It receives the HTTPS connection, decrypts it, and passes plain HTTP to the backend containers. The backend containers themselves never deal with SSL — they receive plain HTTP from Nginx. This is called "TLS termination at the proxy."

### Getting a free SSL certificate (Let's Encrypt)

```bash
# Install certbot on the host machine
apt install certbot

# Get a certificate (your server must be publicly reachable on port 80 first)
certbot certonly --standalone -d yourdomain.com
# Certs go to: /etc/letsencrypt/live/yourdomain.com/
```

### Update nginx/default.conf for HTTPS

Replace the existing `nginx/default.conf` with this:

```nginx
# Redirect all HTTP to HTTPS
server {
    listen 80;
    server_name yourdomain.com;
    return 301 https://$host$request_uri;
}

# HTTPS server
server {
    listen 443 ssl;
    server_name yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    # Security headers
    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Frame-Options DENY;
    add_header X-Content-Type-Options nosniff;

    # VNC viewer (WebSocket — must stay HTTP/1.1 with Upgrade header)
    location /viewer/ {
        proxy_pass http://backend:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
    }

    location /api/ {
        proxy_pass http://backend:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
    }

    location /auth/ {
        proxy_pass http://backend:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }

    location /brain/ {
        rewrite ^/brain/(.*)$ /$1 break;
        proxy_pass http://brain:5000;
        proxy_set_header Host $host;
    }

    location / {
        proxy_pass http://frontend:3000;
        proxy_set_header Host $host;
    }
}
```

### Mount the certs into the Nginx container

In `docker-compose.yml`, add the cert volume to the nginx service:

```yaml
nginx:
  image: nginx:latest
  ports:
    - "80:80"
    - "443:443"        # add this
  volumes:
    - ./nginx/default.conf:/etc/nginx/conf.d/default.conf:ro
    - /etc/letsencrypt:/etc/letsencrypt:ro    # add this
```

### Update backend `.env` for HTTPS

```env
AUTH_COOKIE_SECURE=true        # cookies only sent over HTTPS
CORS_ORIGINS=https://yourdomain.com
```

Then restart:

```bash
docker compose down
docker compose up -d
```

### Certificate auto-renewal

Let's Encrypt certs expire every 90 days. Add a cron job to renew and reload Nginx:

```bash
# Add to root crontab (crontab -e)
0 3 * * * certbot renew --quiet && docker compose -f /opt/tekno-phantom/docker-compose.yml restart nginx
```

---

## 3. Where Your Data Lives and How to Back It Up

All persistent data lives in two Docker named volumes:

| Volume Name | Path Inside Container | What's In It |
|---|---|---|
| `backend_data` | `/app/data/` | All SQLite databases: runs, selectors, recipes, auth users, drag debug log |
| `backend_artifacts` | `/app/artifacts/` | Per-run files: step logs (JSON), failure screenshots (PNG), HTML reports, LLM summaries |

**Why Docker named volumes?** Named volumes persist when you run `docker compose down` — they're not deleted. This means your run history, learned selectors, and HITL recipes survive container restarts and deployments.

**Warning:** They are deleted by `docker compose down --volumes` or `docker volume rm`. Don't run those commands on production without understanding the consequences.

### How to back up data

```bash
# Stop backend briefly so the SQLite files aren't being written during backup
docker compose stop backend

# Back up the data volume (databases and drag log)
docker run --rm \
  -v tekno-phatom-agent_backend_data:/data \
  -v $(pwd)/backups:/backup \
  alpine tar czf /backup/data_$(date +%Y%m%d_%H%M).tar.gz -C /data .

# Back up artifacts (screenshots, logs, reports)
docker run --rm \
  -v tekno-phatom-agent_backend_artifacts:/data \
  -v $(pwd)/backups:/backup \
  alpine tar czf /backup/artifacts_$(date +%Y%m%d_%H%M).tar.gz -C /data .

# Restart backend
docker compose start backend
```

**What this does:** Runs a temporary Alpine Linux container, mounts the Docker volume, and tars everything into a file in the local `backups/` directory.

### How to restore from backup

```bash
docker compose stop backend

# Clear current data and restore from backup
docker run --rm \
  -v tekno-phatom-agent_backend_data:/data \
  -v $(pwd)/backups:/backup \
  alpine sh -c "rm -rf /data/* && tar xzf /backup/data_20260101_0300.tar.gz -C /data"

docker compose start backend
```

### Cleaning up old artifacts

Every run writes 5–20 files to the artifacts volume. After hundreds of runs, this grows large.

```bash
# See how big the artifacts volume is
docker run --rm -v tekno-phatom-agent_backend_artifacts:/data alpine du -sh /data

# Delete artifacts from runs older than 30 days
# (each run is in a subdirectory named by run_id with a timestamp)
docker run --rm -v tekno-phatom-agent_backend_artifacts:/data alpine \
  find /data -maxdepth 1 -type d -mtime +30 -exec rm -rf {} +
```

---

## 4. Updating the System (Rolling Restart)

When you update the code, you want to minimize downtime. Here's the approach:

### Code-only update (no dependency changes)

When you've only changed Python or TypeScript files (not `pyproject.toml` or `package.json`):

```bash
git pull
docker compose up -d --no-deps --build backend brain frontend
```

**What `--no-deps` does:** Rebuilds and restarts only the specified services without touching Nginx. Nginx continues to serve traffic. There will be a brief (~10-30s) downtime while the backend container restarts.

### Dependency update (changed `pyproject.toml` or `package.json`)

```bash
git pull
docker compose up --build -d
```

This rebuilds all images. Longer downtime (~2–5 minutes) while all containers rebuild.

### Quick restart without rebuilding

Just restart the containers without rebuilding (picks up env var changes):

```bash
docker compose restart backend brain
```

### Verify the update worked

```bash
docker compose ps             # all services "running"
curl http://localhost/api/health  # {"status": "ok"}
docker compose logs --tail=30 backend  # no ERROR lines
```

---

## 5. Database Maintenance

All four databases are SQLite files. SQLite does not automatically reclaim disk space when records are deleted — it marks the space as reusable but doesn't shrink the file. The `VACUUM` command rewrites the database to reclaim that space.

```bash
# Enter the backend container
docker compose exec backend bash

# Inside the container:
sqlite3 /app/data/run_store.sqlite3 "VACUUM;"
sqlite3 /app/data/selector_memory.sqlite3 "VACUUM;"
sqlite3 /app/data/recovery_recipes.sqlite3 "VACUUM;"
sqlite3 /app/data/auth.sqlite3 "VACUUM;"
```

**When to do this:** Once a month, or whenever you notice database files are large.

### Remove selectors for apps you no longer test

The selector memory accumulates selectors for every domain ever tested. If you stop testing an app, its selectors just take up space and can cause false positive matches.

```bash
sqlite3 /app/data/selector_memory.sqlite3 \
  "DELETE FROM selector_memory WHERE domain = 'old-app.example.com';"
```

### Trim the drag debug log

The drag debug log appends every drag attempt and never rolls over automatically. On apps with many drag operations, it can grow large.

```bash
# Inside the backend container:
tail -n 10000 /app/data/drag_debug.jsonl > /tmp/drag_trim.jsonl
mv /tmp/drag_trim.jsonl /app/data/drag_debug.jsonl
```

---

## 6. Environment Variable Updates

**To change backend settings** (`.env` values):

```bash
# Edit .env
nano .env

# Restart backend and brain to pick up changes
docker compose up -d --force-recreate backend brain
```

**To change frontend settings** (`NEXT_PUBLIC_*` variables):

Frontend env vars are baked into the JavaScript bundle at build time — you cannot change them without rebuilding the image.

```bash
# Set the new value
export NEXT_PUBLIC_API_BASE_URL=https://newdomain.com

# Rebuild and restart frontend only
docker compose up --build -d --no-deps frontend
```

**JWT secret rotation warning:** If you change `AUTH_JWT_SECRET`, all existing user session cookies become invalid immediately. Every logged-in user will be automatically logged out on their next request. Plan this during off-hours and warn users in advance.

---

## 7. Common Startup Failures and What They Mean

| What you see | What it means | How to fix it |
|---|---|---|
| `ValueError: AUTH_JWT_SECRET must be set` | Auth is enabled but the secret is missing or is a placeholder from `.env.example` | Set a real secret in `.env` |
| `Address already in use` on port 8000 | Another service is using port 8000, or an old container is still running | Run `docker compose down` first |
| Backend exits immediately, no error message | Python dependency import failed | Run `docker compose logs backend` to see the full traceback |
| Frontend shows blank white page | `NEXT_PUBLIC_API_BASE_URL` not set at build time | Rebuild frontend with the correct env var exported |
| Nginx returns `502 Bad Gateway` | A backend service hasn't finished starting yet | Wait 30s and retry; check `docker compose ps` |
| Playwright error: "Executable doesn't exist" | The Chromium browser wasn't installed during the Docker build | Run `docker compose up --build` to rebuild the backend image |
| VNC viewer shows a black screen in the UI | `PLAYWRIGHT_HEADLESS=true` (viewer requires headless=false) | Set `PLAYWRIGHT_HEADLESS=false` in `.env` and restart backend |
| `sqlite3.OperationalError: unable to open database` | The `/app/data/` directory isn't writable | `docker compose exec backend chmod -R 777 /app/data` |
| Brain returns `{"detail": "LLM not configured"}` | `LLM_MODE` is set to `anthropic` or `cloud` but the API key is empty | Set the correct API key in `brain/.env` |
