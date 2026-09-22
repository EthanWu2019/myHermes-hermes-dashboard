# Hermes Dashboard

Real-time operations dashboard for the Hermes Agent fleet on `ethanshermes.com`.

## What it shows

- **Hermes version + commits behind upstream**
- **Up-time** since dashboard backend started
- **Services health matrix** — WebUI, Health, Chat, Ethanos, ComfyUI, Docs, Project, Canvas, Clock, API
- **MiMo quota** (used / total / %)
- **Lifetime stats** — sessions, messages, tool calls, total tokens, cost
- **Daily token rollup chart** (permanent history, sealed at 00:00 CDT)
- **Daily token rollup table** with sealed vs live status

## Architecture

```
Mac Mini (this repo)
├── backend/dashboard.py   FastAPI on :8800, launchd-kept-alive
│   ├── /api/*             JSON endpoints (hermes + services + rollups)
│   └── /                  serves the static frontend build from backend/static/
├── backend/static/        built Next.js artifacts (frontend/out → here)
└── cloudflared tunnel     dashboard.ethanshermes.com → :8800
```

Single-tenant, single-domain, single-machine. Refresh interval 30s. No push notifications.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/healthz` | liveness |
| GET | `/api/snapshot` | full live snapshot (hermes + services) |
| GET | `/api/services` | services only, with up/down summary |
| GET | `/api/rollups` | daily token rollup history (permanent) |
| POST | `/api/refresh` | force a refresh |
| GET | `/` | the dashboard UI |
| GET | `/{any}` | SPA catch-all (returns index.html) |

## Local dev

```bash
cd backend
~/.hermes/hermes-agent/venv/bin/uvicorn dashboard:app --port 8800
```

To rebuild the frontend:

```bash
# (separate worktree or sibling dir)
npx create-next-app@latest frontend --typescript --no-tailwind --app
# ... build the dashboard page.tsx ...
cd frontend
NEXT_PUBLIC_API_URL="" npm run build
rm -rf ../backend/static && mkdir -p ../backend/static
cp -r out/* ../backend/static/
launchctl kickstart -k gui/$(id -u)/ai.ethanwu.hermes-dashboard
```

## Production

Backend is auto-launched by `com.ai.ethanwu.hermes-dashboard.plist` and kept alive by launchd.
Tunnel route `dashboard.ethanshermes.com → :8800` lives in `~/.cloudflared/config.yml`.
