"""HTTP control surface for replay: list recordings, load, play, pause, seek, speed.

Replay is only available when the backend is idle: no recorder running and not
in demo mode. In either of those states every control request is refused with
409, so a replay can never compete with a live recording. When the shared
WebSocket token is configured, the state-changing (POST) endpoints require it
the same way ``/ws`` does; the GET endpoints stay open like ``/health``.

Responses carry ``ReplayInfo`` (state, times, counters) and never a path.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from app.config import get_settings
from app.replay import (
    MAX_SESSION_KEY,
    SPEED_MAX,
    SPEED_MIN,
    RecordingCatalog,
    ReplayError,
    ReplayPlayer,
)
from app.ws import presented_token, token_matches

log = logging.getLogger("app.replay")

router = APIRouter(prefix="/replay", tags=["replay"])

IDLE: dict[str, Any] = {"state": "idle"}


# -- guards ---------------------------------------------------------------------


def current_player(request: Request) -> ReplayPlayer | None:
    return getattr(request.app.state, "replay", None)


def require_available(request: Request) -> None:
    if getattr(request.app.state, "demo", False):
        raise HTTPException(status_code=409, detail="replay is unavailable while demo mode is active")
    if getattr(request.app.state, "recorder", None) is not None:
        raise HTTPException(status_code=409, detail="replay is unavailable while live recording is active")


def require_token(request: Request) -> None:
    expected = get_settings().ws_auth_token.get_secret_value().strip()
    if not expected:
        return
    presented = presented_token(request.query_params.get("token"), request.headers.get("authorization"))
    if not token_matches(presented, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


def require_loaded(request: Request) -> ReplayPlayer:
    player = current_player(request)
    if player is None:
        raise HTTPException(status_code=409, detail="no recording is loaded")
    return player


CONTROL = [Depends(require_available), Depends(require_token)]


# -- bodies ---------------------------------------------------------------------


class LoadBody(BaseModel):
    session_key: int = Field(ge=0, lt=MAX_SESSION_KEY)


class SeekBody(BaseModel):
    position: datetime | None = None
    fraction: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _exactly_one(self) -> SeekBody:
        if (self.position is None) == (self.fraction is None):
            raise ValueError("give exactly one of position or fraction")
        return self


class SpeedBody(BaseModel):
    speed: float = Field(ge=SPEED_MIN, le=SPEED_MAX)


# -- routes ---------------------------------------------------------------------


@router.get("/sessions")
async def list_sessions() -> dict[str, Any]:
    catalog = RecordingCatalog(get_settings().recordings_dir)
    return {"sessions": [summary.model_dump() for summary in catalog.sessions()]}


@router.get("")
async def replay_state(request: Request) -> dict[str, Any]:
    player = current_player(request)
    return player.info().model_dump() if player is not None else IDLE


@router.post("/load", dependencies=CONTROL)
async def load(body: LoadBody, request: Request) -> dict[str, Any]:
    stop_player(request.app)
    try:
        player = ReplayPlayer(get_settings(), body.session_key)
    except ReplayError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    request.app.state.replay = player
    log.info("replay loaded: session %s", body.session_key)
    return player.info().model_dump()


@router.post("/play", dependencies=CONTROL)
async def play(player: ReplayPlayer = Depends(require_loaded)) -> dict[str, Any]:
    player.play()
    return player.info().model_dump()


@router.post("/pause", dependencies=CONTROL)
async def pause(player: ReplayPlayer = Depends(require_loaded)) -> dict[str, Any]:
    player.pause()
    return player.info().model_dump()


@router.post("/seek", dependencies=CONTROL)
async def seek(body: SeekBody, player: ReplayPlayer = Depends(require_loaded)) -> dict[str, Any]:
    player.seek(position=body.position, fraction=body.fraction)
    return player.info().model_dump()


@router.post("/speed", dependencies=CONTROL)
async def speed(body: SpeedBody, player: ReplayPlayer = Depends(require_loaded)) -> dict[str, Any]:
    player.set_speed(body.speed)
    return player.info().model_dump()


@router.post("/unload", dependencies=CONTROL)
async def unload(request: Request) -> dict[str, Any]:
    stop_player(request.app)
    return IDLE


def stop_player(app: Any) -> None:
    """Stop and forget the current player, if any. Safe to call when idle."""
    player = getattr(app.state, "replay", None)
    if player is not None:
        player.stop()
        app.state.replay = None
        log.info("replay unloaded: session %s", player.session_key)
