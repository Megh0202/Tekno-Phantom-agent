# Tekno-Phantom-Agent

Production-ready foundation for a browser automation agent with a decoupled LLM brain service.

## Current Slice (Implemented)
- FastAPI backend with run orchestration APIs
- Standalone brain service for LLM mode selection (`local`/`cloud`)
- Step contract for browser actions and verification actions
- Browser adapter modes: `mock` (default) and real `playwright`
- Next.js UI for multi-step task composition and run monitoring
- SQLite-backed run persistence and artifact logging (`artifacts/<run_id>/...`)

## Repository Layout
- `backend/` API, runtime executor, brain HTTP client, MCP adapter placeholders
- `brain/` standalone LLM service (provider selection and prompt execution)
- `frontend/` Next.js app (task builder + status monitor)
- `docs/` PRD, acceptance criteria, action schema, architecture

## Quick Start

### 1) Environment

```bash
copy .env.example .env
```

Set:
- `BRAIN_BASE_URL=http://localhost:8090`
- `LLM_MODE=local` for vLLM or `LLM_MODE=cloud` for OpenAI (brain service)
- `OPENAI_API_KEY` when using cloud mode

### 2) Brain Service

```bash
cd brain
python -m venv .venv
.venv\Scripts\activate
pip install -e .
uvicorn app.main:app --reload --host 0.0.0.0 --port 8090
```

### 3) Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -e .
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

### 4) Frontend

```bash
cd frontend
copy .env.example .env.local
npm.cmd install
npm.cmd run dev
```

Open `http://localhost:3000`.

### 5) Docker Compose

Copy `.env.example` to `.env` before running Docker so the backend and brain services receive the expected settings.
For the frontend, Docker Compose passes `NEXT_PUBLIC_*` values at build time from the same root `.env`; leaving `NEXT_PUBLIC_API_BASE_URL` blank keeps the UI on same-origin requests through nginx.

## API Endpoints
- `GET /health`
- `GET /api/config`
- `POST /api/runs`
- `GET /api/runs`
- `GET /api/runs/{run_id}`
- `POST /api/runs/{run_id}/cancel`

## Notes
- Real browser execution requires `BROWSER_MODE=playwright` and `python -m playwright install chromium`.
- File MCP transport is still placeholder; local artifact file writes are implemented.
- Provider toggle is brain-service admin config only; UI exposes it as read-only status.
