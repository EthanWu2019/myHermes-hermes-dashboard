"""
Hermes Dashboard backend — FastAPI single-file service.

Responsibilities
----------------
1. Probe localhost hermes endpoints every 30s; cache latest snapshot.
2. Expose JSON API to the public dashboard frontend.
3. Persist daily token rollups to SQLite at 23:59 local time (CDT).

Run:
    uvicorn dashboard:app --host 0.0.0.0 --port 8800

Env:
    HERMES_API_URL   default http://localhost:8080/api/status
    SERVICES_JSON    JSON map of {label: {name, url, critical, icon}}
                     override the built-in service map
    DB_PATH          default /Users/ethanwu/workspace/hermes-dashboard/backend/dashboard.db
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
CDT = timezone(timedelta(hours=-5))  # America/Chicago (CST=UTC-6, CDT=UTC-5)
# For real production use we should derive from system; keep static for now
# since the entire host runs in America/Chicago.

HERMES_API_URL = os.environ.get("HERMES_API_URL", "http://localhost:8080/api/status")
DB_PATH = Path(os.environ.get("DB_PATH", str(Path(__file__).parent / "dashboard.db")))
LOG = logging.getLogger("hermes-dashboard")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# -----------------------------------------------------------------------------
# Static service map. These are the always-on ethanshermes.com tunnel backends.
# `critical` flags are surfaced visually; they don't trigger push notifications
# (per owner's design — dashboard is read-only by default).
# -----------------------------------------------------------------------------
SERVICES_DEFAULT: list[dict[str, Any]] = [
    {"label": "WebUI",      "host": "ethanshermes.com",      "url": "https://ethanshermes.com",      "local": "http://localhost:8080", "critical": True,  "icon": "webui"},
    {"label": "Health",     "host": "health.ethanshermes.com","url": "https://health.ethanshermes.com","local": "http://localhost:8089", "critical": True,  "icon": "health"},
    {"label": "Chat",       "host": "chat.ethanshermes.com",  "url": "https://chat.ethanshermes.com",  "local": "http://localhost:8787", "critical": True,  "icon": "chat"},
    {"label": "Ethanos",    "host": "ethanos.ethanshermes.com","url": "https://ethanos.ethanshermes.com","local": "http://localhost:3100", "critical": False, "icon": "ethanos"},
    {"label": "ComfyUI",    "host": "comfyui.ethanshermes.com","url": "https://comfyui.ethanshermes.com","local": "http://localhost:8188", "critical": False, "icon": "comfyui"},
    {"label": "Docs",       "host": "docs.ethanshermes.com",  "url": "https://docs.ethanshermes.com",  "local": "http://localhost:8765", "critical": False, "icon": "docs"},
    {"label": "Project",    "host": "project.ethanshermes.com","url": "https://project.ethanshermes.com","local": "http://localhost:8766", "critical": False, "icon": "project"},
    {"label": "Canvas",     "host": "canvas.ethanshermes.com", "url": "https://canvas.ethanshermes.com", "local": "http://localhost:8770", "critical": False, "icon": "canvas"},
    {"label": "Clock",      "host": "clock.ethanshermes.com",  "url": "https://clock.ethanshermes.com",  "local": "http://localhost:8771", "critical": True,  "icon": "clock"},
    {"label": "API",        "host": "api.ethanshermes.com",    "url": "https://api.ethanshermes.com",    "local": "http://localhost:8000", "critical": True,  "icon": "api"},
]

# -----------------------------------------------------------------------------
# SQLite schema
# -----------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_rollup (
    date_local TEXT PRIMARY KEY,           -- YYYY-MM-DD (CDT)
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    rollup_complete INTEGER NOT NULL DEFAULT 0, -- 0 = in-progress day, 1 = sealed
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rollup_complete ON daily_rollup(rollup_complete);
"""


def db_init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def db_upsert_today(stats: dict[str, Any], date_local: str, sealed: bool) -> None:
    """Insert or update today's rollup row. If `sealed`, mark rollup_complete=1."""
    now_iso = datetime.now(CDT).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO daily_rollup
              (date_local, input_tokens, output_tokens, cache_read_tokens,
               cache_write_tokens, reasoning_tokens, total_tokens,
               estimated_cost_usd, rollup_complete, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date_local) DO UPDATE SET
              input_tokens = excluded.input_tokens,
              output_tokens = excluded.output_tokens,
              cache_read_tokens = excluded.cache_read_tokens,
              cache_write_tokens = excluded.cache_write_tokens,
              reasoning_tokens = excluded.reasoning_tokens,
              total_tokens = excluded.total_tokens,
              estimated_cost_usd = excluded.estimated_cost_usd,
              rollup_complete = MAX(rollup_complete, excluded.rollup_complete)
            """,
            (
                date_local,
                int(stats.get("input_tokens", 0)),
                int(stats.get("output_tokens", 0)),
                int(stats.get("cache_read_tokens", 0)),
                int(stats.get("cache_write_tokens", 0)),
                int(stats.get("reasoning_tokens", 0)),
                int(stats.get("total_tokens", 0)),
                float(stats.get("estimated_cost_usd", 0)),
                1 if sealed else 0,
                now_iso,
            ),
        )
        conn.commit()


def db_get_all_rollups() -> list[dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM daily_rollup ORDER BY date_local ASC"
        ).fetchall()
    return [dict(r) for r in rows]


# -----------------------------------------------------------------------------
# Live state — held in memory, refreshed by background task
# -----------------------------------------------------------------------------
class ServiceState(BaseModel):
    label: str
    host: str
    url: str
    local: str
    critical: bool
    icon: str
    status: str = "unknown"          # up | down | unknown
    http_code: int | None = None
    latency_ms: int | None = None
    last_checked: str | None = None
    error: str | None = None


class HermesState(BaseModel):
    version: str = "unknown"
    upstream_version: str | None = None
    commits_behind: int | None = None
    install_method: str | None = None
    python: str | None = None
    started_at: str | None = None
    uptime_seconds: int | None = None
    model: str | None = None
    provider: str | None = None
    summary: dict[str, Any] = {}
    mimo_used: int | None = None
    mimo_total: int | None = None
    timestamp: str | None = None


class Snapshot(BaseModel):
    hermes: HermesState
    services: list[ServiceState]
    last_full_refresh: str | None = None


SNAPSHOT = Snapshot(hermes=HermesState(), services=[ServiceState(**s) for s in SERVICES_DEFAULT])

# -----------------------------------------------------------------------------
# Background probes
# -----------------------------------------------------------------------------
def _detect_hermes_version() -> dict[str, Any]:
    """Read version + .update_check + .install_method in one shot."""
    out: dict[str, Any] = {}
    try:
        out["version"] = subprocess.check_output(
            ["hermes", "--version"], text=True, stderr=subprocess.STDOUT, timeout=5
        ).strip().split("\n")[0]
    except Exception as e:
        out["version_error"] = str(e)

    update_path = Path.home() / ".hermes" / ".update_check"
    if update_path.exists():
        try:
            data = json.loads(update_path.read_text())
            out["commits_behind"] = data.get("behind")
        except Exception as e:
            out["update_check_error"] = str(e)

    install_path = Path.home() / ".hermes" / ".install_method"
    if install_path.exists():
        out["install_method"] = install_path.read_text().strip()

    return out


async def _probe_hermes_api() -> dict[str, Any]:
    """Hit /api/status. Returns the parsed JSON body or {'error': ...}."""
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            r = await client.get(HERMES_API_URL)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}


async def _probe_one_service(svc: dict[str, Any]) -> ServiceState:
    """Probe a service via its local URL (faster than going through the tunnel)."""
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=5, follow_redirects=True) as client:
        try:
            r = await client.get(svc["local"])
            latency = int((time.perf_counter() - started) * 1000)
            return ServiceState(
                label=svc["label"],
                host=svc["host"],
                url=svc["url"],
                local=svc["local"],
                critical=svc["critical"],
                icon=svc["icon"],
                status="up" if r.status_code < 500 else "down",
                http_code=r.status_code,
                latency_ms=latency,
                last_checked=datetime.now(CDT).isoformat(),
            )
        except Exception as e:
            latency = int((time.perf_counter() - started) * 1000)
            return ServiceState(
                label=svc["label"],
                host=svc["host"],
                url=svc["url"],
                local=svc["local"],
                critical=svc["critical"],
                icon=svc["icon"],
                status="down",
                http_code=None,
                latency_ms=latency,
                last_checked=datetime.now(CDT).isoformat(),
                error=f"{type(e).__name__}: {e}",
            )


async def _probe_all_services() -> list[ServiceState]:
    return await asyncio.gather(*[_probe_one_service(s) for s in SERVICES_DEFAULT])


async def refresh_snapshot() -> None:
    """Refresh the in-memory SNAPSHOT — runs every 30s."""
    global SNAPSHOT

    hermes_meta = _detect_hermes_version()
    api_data = await _probe_hermes_api()
    services = await _probe_all_services()

    version_full = hermes_meta.get("version", "unknown")
    version_clean = version_full.replace("Hermes Agent v", "").split(" ")[0]

    summary = api_data.get("summary", {}) if "error" not in api_data else {}
    started_at = SNAPSHOT.hermes.started_at or datetime.now(CDT).isoformat()
    uptime = int((datetime.now(CDT) - datetime.fromisoformat(started_at)).total_seconds())

    SNAPSHOT = Snapshot(
        hermes=HermesState(
            version=version_clean,
            upstream_version=None,
            commits_behind=hermes_meta.get("commits_behind"),
            install_method=hermes_meta.get("install_method"),
            python=hermes_meta.get("python"),
            started_at=started_at,
            uptime_seconds=uptime,
            model=api_data.get("model"),
            provider=api_data.get("provider"),
            summary=summary,
            mimo_used=api_data.get("mimo_used"),
            mimo_total=api_data.get("mimo_total"),
            timestamp=api_data.get("timestamp") or datetime.now(CDT).isoformat(),
        ),
        services=services,
        last_full_refresh=datetime.now(CDT).isoformat(),
    )
    LOG.info(
        "snapshot refreshed: hermes=%s services_up=%d/%d",
        version_clean,
        sum(1 for s in services if s.status == "up"),
        len(services),
    )


# -----------------------------------------------------------------------------
# Daily rollup loop — fires at local midnight (00:00 CDT)
# -----------------------------------------------------------------------------
async def daily_rollup_loop() -> None:
    """
    Every minute, check the wall clock. When local time crosses 00:00 CDT,
    seal yesterday's row and reset today's row.
    """
    last_sealed_date: str | None = None
    while True:
        try:
            now = datetime.now(CDT)
            today = now.strftime("%Y-%m-%d")
            if last_sealed_date != today and now.hour == 0 and now.minute < 2:
                # We just rolled into a new day — seal yesterday's rollup using
                # the cumulative snapshot. Daily-rollup semantics: one row per day,
                # value = cumulative at end-of-day.
                yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
                stats = SNAPSHOT.hermes.summary or {}
                if stats:
                    db_upsert_today(stats, yesterday, sealed=True)
                    LOG.info("sealed daily rollup for %s", yesterday)
                last_sealed_date = today

            # Always refresh today's row with current cumulative (rollup_complete=0)
            stats = SNAPSHOT.hermes.summary or {}
            if stats:
                db_upsert_today(stats, today, sealed=False)
        except Exception as e:
            LOG.exception("daily rollup loop error: %s", e)
        await asyncio.sleep(60)


# -----------------------------------------------------------------------------
# FastAPI app
# -----------------------------------------------------------------------------
app = FastAPI(title="Hermes Dashboard", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup() -> None:
    db_init()
    await refresh_snapshot()
    asyncio.create_task(_periodic_refresh())
    asyncio.create_task(daily_rollup_loop())
    LOG.info("hermes-dashboard backend started on :8800")


async def _periodic_refresh() -> None:
    while True:
        await asyncio.sleep(30)
        try:
            await refresh_snapshot()
        except Exception as e:
            LOG.exception("refresh_snapshot error: %s", e)


# -----------------------------------------------------------------------------
# API endpoints
# -----------------------------------------------------------------------------
@app.get("/api/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "running",
        "service": "Hermes Dashboard Backend",
        "snapshot_age_seconds": (
            int((datetime.now(CDT) - datetime.fromisoformat(SNAPSHOT.last_full_refresh)).total_seconds())
            if SNAPSHOT.last_full_refresh
            else None
        ),
        "timestamp": datetime.now(CDT).isoformat(),
    }


@app.get("/api/snapshot")
async def snapshot() -> Snapshot:
    return SNAPSHOT


@app.get("/api/rollups")
async def rollups() -> dict[str, Any]:
    rows = db_get_all_rollups()
    return {"rollups": rows, "count": len(rows)}


@app.get("/api/services")
async def services() -> dict[str, Any]:
    return {
        "services": [s.model_dump() for s in SNAPSHOT.services],
        "summary": {
            "total": len(SNAPSHOT.services),
            "up": sum(1 for s in SNAPSHOT.services if s.status == "up"),
            "down": sum(1 for s in SNAPSHOT.services if s.status == "down"),
            "critical_down": [
                s.label for s in SNAPSHOT.services if s.critical and s.status == "down"
            ],
        },
    }


@app.post("/api/refresh")
async def force_refresh() -> dict[str, str]:
    await refresh_snapshot()
    return {"status": "ok"}


# -----------------------------------------------------------------------------
# Static frontend (served from /static — built artifacts from `frontend/out/`)
# -----------------------------------------------------------------------------
STATIC_DIR = Path(__file__).parent / "static"

if STATIC_DIR.exists():
    # Assets directory (Next.js _next/)
    _next_dir = STATIC_DIR / "_next"
    if _next_dir.exists():
        app.mount("/_next", StaticFiles(directory=_next_dir), name="next-assets")

    @app.get("/")
    async def root_index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/{path:path}")
    async def catch_all(path: str) -> FileResponse:
        # Try the requested file first (assets like favicon.ico, *.svg)
        candidate = STATIC_DIR / path
        if candidate.is_file():
            return FileResponse(candidate)
        # Fall back to index.html for client-side routing
        return FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8800, log_level="info")
