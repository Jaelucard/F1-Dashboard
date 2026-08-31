"""FastAPI application entrypoint.

Run with:  make dev-backend
           .venv/bin/uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from app import ws
from app.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("f1dash")

STARTED_AT = datetime.now(timezone.utc)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    log.info("backend starting, live_mode=%s", settings.live_mode)
    if settings.credentials_present:
        log.info("OpenF1 credentials loaded from backend/.env")
    else:
        log.warning(
            "OpenF1 credentials not set. Live mode will refuse to start. "
            "Copy backend/.env.example to backend/.env and fill it in."
        )
    log.info("recordings will be written under %s", settings.recordings_dir)
    yield
    log.info("backend shutting down")


app = FastAPI(
    title="F1 Timing Dashboard",
    description=(
        "Unofficial live timing dashboard. Data from the OpenF1 API "
        "(https://openf1.org). Not affiliated with Formula 1."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# The Vite dev server runs on a different origin, so the browser needs this for
# the /health fetch. The WebSocket handshake is not subject to CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

@app.get("/health")
async def health() -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ok",
        "version": app.version,
        "phase": 0,
        "live_mode": settings.live_mode,
        "credentials_present": settings.credentials_present,
        "started_at": STARTED_AT.isoformat(timespec="seconds"),
        "browsers_connected": ws.manager.count,
    }


@app.websocket("/ws")
async def websocket_route(websocket: WebSocket) -> None:
    settings = get_settings()
    await ws.websocket_endpoint(
        websocket,
        live_mode=settings.live_mode,
        credentials_present=settings.credentials_present,
    )
