"""
Hermes Dashboard backend — FastAPI single-file service.

Responsibilities
----------------
1. Probe localhost hermes endpoints every 30s; cache latest snapshot.
2. Expose JSON API to the public dashboard frontend.
3. Serve the static Next.js frontend from /.

Data sources
------------
- Live state: hermes --version, .update_check, .install_method, config.yaml model.default
- Live token snapshot: http://localhost:8080/api/status (last-call view, used only for MiMo quota)
- Daily token rollups: ~/.hermes/token_stats/YYYY-MM-DD.json (authoritative — produced by token_tracker.py cron)
- Cumulative summary: ~/.hermes/token_stats/summary.json (produced by token_tracker.py)

Run:
    uvicorn dashboard:app --host 0.0.0.0 --port 8800
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
CDT = timezone(timedelta(hours=-5))  # America/Chicago (CDT=UTC-5)

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
HERMES_API_URL = os.environ.get("HERMES_API_URL", "http://localhost:8080/api/status")
TOKEN_STATS_DIR = HERMES_HOME / "token_stats"
CONFIG_PATH = HERMES_HOME / "config.yaml"
STATIC_DIR = Path(__file__).parent / "static"

LOG = logging.getLogger("hermes-dashboard")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# -----------------------------------------------------------------------------
# Service map — owner-curated (2026-09-21).
# Critical services are surfaced visually; no push notifications.
# Owner explicitly removed: Docs, ComfyUI, Project.
# -----------------------------------------------------------------------------
SERVICES_DEFAULT: list[dict[str, Any]] = [
    {"label": "WebUI",   "host": "ethanshermes.com",      "url": "https://ethanshermes.com",      "local": "http://localhost:8080", "critical": True,  "icon": "webui",
     "note": "Hermes WebUI main entry"},
    {"label": "Health",  "host": "health.ethanshermes.com","url": "https://health.ethanshermes.com","local": "http://localhost:8089", "critical": True,  "icon": "health",
     "note": "Apple Health data webhook API"},
    {"label": "Chat",    "host": "chat.ethanshermes.com",  "url": "https://chat.ethanshermes.com",  "local": "http://localhost:8787", "critical": True,  "icon": "chat",
     "note": "HaiMian chat (Hermes gateway)"},
    {"label": "Ethanos", "host": "ethanos.ethanshermes.com","url": "https://ethanos.ethanshermes.com","local": "http://localhost:3100", "critical": False, "icon": "ethanos",
     "note": "ethanos (kept for now — owner to confirm)"},
    {"label": "Canvas",  "host": "canvas.ethanshermes.com", "url": "https://canvas.ethanshermes.com", "local": "http://localhost:8770", "critical": False, "icon": "canvas",
     "note": "canvas widget"},
    {"label": "Clock",   "host": "clock.ethanshermes.com",  "url": "https://clock.ethanshermes.com",  "local": "http://localhost:8771", "critical": True,  "icon": "clock",
     "note": "mac-clock iOS StandBy 21:9 dock display"},
    {"label": "API",     "host": "api.ethanshermes.com",    "url": "https://api.ethanshermes.com",    "local": "http://localhost:8000", "critical": True,  "icon": "api",
     "note": "NexusAgent capstone backend (FastAPI)"},
]


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
    note: str = ""
    status: str = "unknown"          # up | down | unknown
    http_code: int | None = None
    latency_ms: int | None = None
    last_checked: str | None = None
    error: str | None = None


class HermesState(BaseModel):
    version: str = "unknown"
    commits_behind: int | None = None
    install_method: str | None = None
    started_at: str | None = None
    uptime_seconds: int | None = None
    # Authoritative model — read from config.yaml, NOT from /api/status
    # (which reports last-call model, not default).
    configured_model: str | None = None
    configured_provider: str | None = None
    # Optional context (last-call view from /api/status).
    last_call_model: str | None = None
    last_call_provider: str | None = None
    summary: dict[str, Any] = {}
    mimo_used: int | None = None
    mimo_total: int | None = None
    timestamp: str | None = None


class Snapshot(BaseModel):
    hermes: HermesState
    services: list[ServiceState]
    last_full_refresh: str | None = None


SNAPSHOT = Snapshot(
    hermes=HermesState(),
    services=[ServiceState(**{k: s[k] for k in ("label", "host", "url", "local", "critical", "icon", "note")}) for s in SERVICES_DEFAULT],
)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _read_configured_model() -> tuple[str | None, str | None]:
    """Return (model.default, provider) from ~/.hermes/config.yaml.
    Falls back to (None, None) if config is missing or malformed."""
    if not CONFIG_PATH.exists():
        return (None, None)
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        m = cfg.get("model") or {}
        return (m.get("default"), m.get("provider"))
    except Exception as e:
        LOG.warning("config.yaml read failed: %s", e)
        return (None, None)


def _detect_hermes_version() -> dict[str, Any]:
    """Read hermes version, .update_check, .install_method."""
    out: dict[str, Any] = {}
    try:
        raw = subprocess.check_output(["hermes", "--version"], text=True, stderr=subprocess.STDOUT, timeout=5).strip()
        first = raw.split("\n")[0]
        # Format: "Hermes Agent v0.21.4 (2026.9.21) · upstream 524041b9"
        m = re.search(r"v(\d+\.\d+\.\d+)", first)
        out["version"] = m.group(1) if m else first
    except Exception as e:
        out["version_error"] = str(e)

    update_path = HERMES_HOME / ".update_check"
    if update_path.exists():
        try:
            data = json.loads(update_path.read_text())
            out["commits_behind"] = data.get("behind")
        except Exception as e:
            LOG.warning("update_check read failed: %s", e)

    install_path = HERMES_HOME / ".install_method"
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
                label=svc["label"], host=svc["host"], url=svc["url"], local=svc["local"],
                critical=svc["critical"], icon=svc["icon"], note=svc.get("note", ""),
                status="up" if r.status_code < 500 else "down",
                http_code=r.status_code, latency_ms=latency,
                last_checked=datetime.now(CDT).isoformat(),
            )
        except Exception as e:
            latency = int((time.perf_counter() - started) * 1000)
            return ServiceState(
                label=svc["label"], host=svc["host"], url=svc["url"], local=svc["local"],
                critical=svc["critical"], icon=svc["icon"], note=svc.get("note", ""),
                status="down", http_code=None, latency_ms=latency,
                last_checked=datetime.now(CDT).isoformat(),
                error=f"{type(e).__name__}: {e}",
            )


async def _probe_all_services() -> list[ServiceState]:
    return await asyncio.gather(*[_probe_one_service(s) for s in SERVICES_DEFAULT])


async def refresh_snapshot() -> None:
    """Refresh the in-memory SNAPSHOT — runs every 30s."""
    global SNAPSHOT

    hermes_meta = _detect_hermes_version()
    cfg_model, cfg_provider = _read_configured_model()
    api_data = await _probe_hermes_api()
    services = await _probe_all_services()

    # Preserve the original started_at across refreshes so uptime keeps growing.
    started_at = SNAPSHOT.hermes.started_at or datetime.now(CDT).isoformat()
    uptime = int((datetime.now(CDT) - datetime.fromisoformat(started_at)).total_seconds())

    summary = api_data.get("summary", {}) if "error" not in api_data else {}

    SNAPSHOT = Snapshot(
        hermes=HermesState(
            version=hermes_meta.get("version", "unknown"),
            commits_behind=hermes_meta.get("commits_behind"),
            install_method=hermes_meta.get("install_method"),
            started_at=started_at,
            uptime_seconds=uptime,
            configured_model=cfg_model,
            configured_provider=cfg_provider,
            last_call_model=api_data.get("model"),
            last_call_provider=api_data.get("provider"),
            summary=summary,
            mimo_used=api_data.get("mimo_used"),
            mimo_total=api_data.get("mimo_total"),
            timestamp=api_data.get("timestamp") or datetime.now(CDT).isoformat(),
        ),
        services=services,
        last_full_refresh=datetime.now(CDT).isoformat(),
    )
    LOG.info(
        "snapshot refreshed: hermes=%s model=%s/%s services_up=%d/%d",
        SNAPSHOT.hermes.version,
        cfg_model or "?",
        cfg_provider or "?",
        sum(1 for s in services if s.status == "up"),
        len(services),
    )


# -----------------------------------------------------------------------------
# Daily token rollups — read from ~/.hermes/token_stats/, NOT a separate SQLite.
# Authoritative source: token_tracker.py cron (job 253c942cc597, 23:55 daily).
# -----------------------------------------------------------------------------
def _read_all_daily_rollups() -> list[dict[str, Any]]:
    """Return list of daily rollup dicts (one per date, newest last).
    Each dict matches the shape of ~/.hermes/token_stats/YYYY-MM-DD.json."""
    if not TOKEN_STATS_DIR.exists():
        return []
    rows: list[dict[str, Any]] = []
    for f in sorted(TOKEN_STATS_DIR.glob("20*.json")):
        if f.name == "summary.json":
            continue
        try:
            rows.append(json.loads(f.read_text()))
        except Exception as e:
            LOG.warning("read %s failed: %s", f.name, e)
    return rows


def _read_summary() -> dict[str, Any] | None:
    sp = TOKEN_STATS_DIR / "summary.json"
    if not sp.exists():
        return None
    try:
        return json.loads(sp.read_text())
    except Exception:
        return None


# -----------------------------------------------------------------------------
# FastAPI app
# -----------------------------------------------------------------------------
app = FastAPI(title="Hermes Dashboard", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup() -> None:
    await refresh_snapshot()
    asyncio.create_task(_periodic_refresh())
    LOG.info("hermes-dashboard backend started on :8800 (rollups from %s)", TOKEN_STATS_DIR)


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
        "rollup_source": str(TOKEN_STATS_DIR),
        "rollup_files": sum(1 for _ in TOKEN_STATS_DIR.glob("20*.json") if _.name != "summary.json") if TOKEN_STATS_DIR.exists() else 0,
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
    """Return daily rollups (authoritative: token_tracker.py cron output)."""
    rows = _read_all_daily_rollups()
    summary = _read_summary()
    # Flatten each row to a flat shape for the frontend.
    flat = []
    for r in rows:
        s = r.get("summary", {})
        flat.append({
            "date_local": r["date"],
            "input_tokens": s.get("input_tokens", 0),
            "output_tokens": s.get("output_tokens", 0),
            "cache_read_tokens": s.get("cache_read_tokens", 0),
            "cache_write_tokens": s.get("cache_write_tokens", 0),
            "reasoning_tokens": s.get("reasoning_tokens", 0),
            "total_tokens": s.get("total_tokens", 0),
            "estimated_cost_usd": s.get("estimated_cost_usd", 0),
            "total_sessions": s.get("total_sessions", 0),
            "total_messages": s.get("total_messages", 0),
            "total_tool_calls": s.get("total_tool_calls", 0),
            "by_model": r.get("by_model", {}),
            "generated_at": r.get("generated_at"),
            "source": "token_tracker.py",
        })
    # Mark the most recent date as "live" if it equals today (CDT).
    today = datetime.now(CDT).strftime("%Y-%m-%d")
    if flat and flat[-1]["date_local"] == today:
        flat[-1]["rollup_complete"] = 0
    elif flat:
        flat[-1]["rollup_complete"] = 1
    return {
        "rollups": flat,
        "count": len(flat),
        "summary": summary,
        "today_local": today,
    }


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
# Static frontend (served from backend/static/)
# -----------------------------------------------------------------------------
if STATIC_DIR.exists():
    _next_dir = STATIC_DIR / "_next"
    if _next_dir.exists():
        app.mount("/_next", StaticFiles(directory=_next_dir), name="next-assets")

    @app.get("/")
    async def root_index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/{path:path}")
    async def catch_all(path: str) -> FileResponse:
        candidate = STATIC_DIR / path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8800, log_level="info")
