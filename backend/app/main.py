"""FastAPI application entrypoint.

Run with:  make dev-backend
           .venv/bin/uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import PHASE, ws
from app.adapters.openf1_live import OpenF1LiveSource
from app.config import get_settings
from app.feed import compute_feed, evaluate_health
from app.models import FeedInfo, RecorderInfo, SessionState
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
    source: OpenF1LiveSource | None = None

    if settings.demo_mode:
        # Imported here, not at module scope: sample data is a development
        # convenience and must not be a dependency of the production path.
        from scripts.sample_data import build_source

        source = build_source()
        log.warning(
            "DEMO_MODE is on: serving a synthetic grid, NOT connecting to OpenF1. "
            "Turn it off in backend/.env before the session."
        )
        app.state.recorder = None
        app.state.source = source
        app.state.demo = True
        yield
        log.info("backend shutting down")
        return

    app.state.demo = False
    if settings.live_mode:
        if not settings.credentials_present:
            log.error(
                "LIVE_MODE is true but credentials are missing. Set OPENF1_USERNAME "
                "and OPENF1_PASSWORD in backend/.env. Starting without the recorder."
            )
        else:
            recorder = RawRecorder(settings)
            # The live adapter rides on the recorder's stream instead of
            # opening a second MQTT connection. Registered before start() so
            # no message can slip past between connecting and subscribing.
            source = OpenF1LiveSource(credentials_present=True)
            recorder.add_subscriber(source.on_raw_message)
            try:
                recorder.start()
            except Exception:
                log.exception("recorder failed to start; the API stays up")
                recorder = None
                source = None
    else:
        log.info("LIVE_MODE is false - not connecting to OpenF1")

    app.state.recorder = recorder
    app.state.source = source
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
# the /health fetch. The WebSocket handshake is not subject to CORS; ws.py
# checks Origin itself.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _current_recorder() -> RawRecorder | None:
    return getattr(app.state, "recorder", None)


def _current_source() -> OpenF1LiveSource | None:
    return getattr(app.state, "source", None)


def _is_demo() -> bool:
    return bool(getattr(app.state, "demo", False))


def _evaluate() -> dict[str, Any]:
    """Everything /health, /ready and the snapshot share about backend state."""
    settings = get_settings()
    recorder = _current_recorder()
    health = recorder.health() if recorder is not None else None
    # A running recorder means live data is expected, whatever the flag says.
    live = (settings.live_mode or recorder is not None) and not _is_demo()
    feed: FeedInfo | None = None
    if live:
        feed = compute_feed(health, stale_after=settings.feed_stale_seconds)
    status, ready, problems = evaluate_health(
        live_mode=live,
        demo_mode=_is_demo(),
        # start() refuses without credentials, so a running recorder had them.
        credentials_present=settings.credentials_present or recorder is not None,
        recorder=health,
        feed=feed,
    )
    return {
        "status": status,
        "ready": ready,
        "problems": problems,
        "recorder_health": health,
        "feed": feed,
    }


@app.get("/health")
async def health() -> dict[str, object]:
    """Liveness plus everything needed to answer "is it working?" without the logs.

    Always 200: this process answering *is* the liveness signal. ``status`` and
    ``problems`` say whether it is actually doing its job; ``/ready`` turns the
    same evaluation into a status code for anything that polls.
    """
    settings = get_settings()
    evaluated = _evaluate()
    source = _current_source()
    feed = evaluated["feed"]
    return {
        "status": evaluated["status"],
        "ready": evaluated["ready"],
        "problems": evaluated["problems"],
        "version": app.version,
        "phase": PHASE,
        "live_mode": settings.live_mode,
        "demo_mode": settings.demo_mode,
        "credentials_present": settings.credentials_present,
        "started_at": STARTED_AT.isoformat(timespec="seconds"),
        "browsers_connected": ws.manager.count,
        "websocket": ws.manager.stats(),
        "feed": feed.model_dump() if feed is not None else None,
        "feed_stale_seconds": settings.feed_stale_seconds,
        "recorder": evaluated["recorder_health"],
        "source": source.stats() if source is not None else None,
    }


@app.get("/ready")
async def ready() -> JSONResponse:
    """503 until the backend can do what it was configured to do."""
    evaluated = _evaluate()
    body = {"ready": evaluated["ready"], "status": evaluated["status"], "problems": evaluated["problems"]}
    return JSONResponse(body, status_code=200 if evaluated["ready"] else 503)


def build_snapshot() -> SessionState:
    """Assemble the current SessionState from whichever source is running."""
    settings = get_settings()
    source = _current_source()
    if source is None:
        return ws.idle_snapshot(settings.credentials_present)

    if _is_demo():
        return source.snapshot(mode="demo")

    recorder = _current_recorder()
    info = None
    health = recorder.health() if recorder is not None else None
    if health is not None:
        info = RecorderInfo(
            connected=health["connected"],
            messages_recorded=health["messages_recorded"],
            last_message_at=health["last_message_at"],
            topics=health["topics"],
            token_expires_at=health["token_expires_at"],
            last_error=health["last_error"],
            recording_ok=health["recording_ok"],
            write_failures=health["write_failures"],
            fanout_dropped=health["fanout_dropped"],
            messages_possibly_lost=health["messages_possibly_lost"],
            may_be_incomplete=health["may_be_incomplete"],
            disk_free_bytes=health["disk_free_bytes"],
            disk_low=health["disk_low"],
        )
    state = source.snapshot(mode="live", recorder=info)
    state.feed = compute_feed(health, stale_after=settings.feed_stale_seconds)
    return state


@app.websocket("/ws")
async def websocket_route(websocket: WebSocket) -> None:
    await ws.websocket_endpoint(websocket, build_snapshot)
