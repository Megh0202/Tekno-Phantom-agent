# Frontend

## Setup

```bash
npm.cmd install
copy .env.example .env.local
npm.cmd run dev
```

App runs at `http://localhost:3000`.

`NEXT_PUBLIC_API_BASE_URL` can stay empty to use the current origin, which is the safest default behind nginx/Docker Compose.
