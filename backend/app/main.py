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

from app import PHASE, ws
from app.config import get_settings
from app.recorder import RawRecorder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("f1dash")

STARTED_AT = datetime.now(timezone.utc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the recorder alongside the API when live mode is on.

    The recorder deliberately lives *inside* this process. Phase 2's live
    adapter subscribes to it in-process rather than opening a second MQTT
    connection, which is what the OpenF1 docs ask for. If you would rather
    record with nothing else in the way - the Tier A fallback - run
    `make record` instead of `make dev`, and do not run both: that would open
    two connections and write the same messages twice.
    """
    settings = get_settings()
    log.info("backend starting, live_mode=%s", settings.live_mode)
    log.info("recordings will be written under %s", settings.recordings_dir)

    recorder: RawRecorder | None = None
    if settings.live_mode:
        if not settings.credentials_present:
            log.error(
                "LIVE_MODE is true but credentials are missing. Set OPENF1_USERNAME "
                "and OPENF1_PASSWORD in backend/.env. Starting without the recorder."
            )
        else:
            recorder = RawRecorder(settings)
            try:
                recorder.start()
            except Exception:
                log.exception("recorder failed to start; the API stays up")
                recorder = None
    else:
        log.info("LIVE_MODE is false - not connecting to OpenF1")

    app.state.recorder = recorder
    yield

    if recorder is not None:
        recorder.stop()
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
    """Everything needed to answer "is it working?" without reading the logs."""
    settings = get_settings()
    recorder: RawRecorder | None = getattr(app.state, "recorder", None)
    return {
        "status": "ok",
        "version": app.version,
        "phase": PHASE,
        "live_mode": settings.live_mode,
        "credentials_present": settings.credentials_present,
        "started_at": STARTED_AT.isoformat(timespec="seconds"),
        "browsers_connected": ws.manager.count,
        "recorder": recorder.health() if recorder else None,
    }


@app.websocket("/ws")
async def websocket_route(websocket: WebSocket) -> None:
    settings = get_settings()
    await ws.websocket_endpoint(
        websocket,
        live_mode=settings.live_mode,
        credentials_present=settings.credentials_present,
    )
