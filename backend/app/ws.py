"""The single WebSocket fan-out to connected browsers.

Every mode pushes through this one path, so the browser cannot tell live from
replay from historical: it receives a ``SessionState`` and renders it.

The push is a fixed-rate snapshot rather than a per-message diff. At a few
hundred messages a second a diff stream would mean a React render per message,
and a browser that falls behind can never catch up. A whole snapshot at a
steady 1 Hz is small (20-ish drivers), always self-consistent, and lets a
browser that reconnects mid-session be correct immediately with no replay of
missed deltas. The steady beat is also what makes the data-age counter
meaningful: if snapshots stop arriving, the backend is gone.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import WebSocket, WebSocketDisconnect

from app import PHASE
from app.models import SessionState

log = logging.getLogger(__name__)

PUSH_INTERVAL_SECONDS = 1.0

SnapshotProvider = Callable[[], SessionState]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ConnectionManager:
    """Tracks open browser sockets and broadcasts to all of them."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
        log.info("browser connected (%d open)", len(self._connections))

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)
        log.info("browser disconnected (%d open)", len(self._connections))

    @property
    def count(self) -> int:
        return len(self._connections)

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Send to every open socket, dropping any that have died.

        Serialise once rather than per-connection: the snapshot is the biggest
        thing we send, and re-encoding it per browser is waste.
        """
        payload = json.dumps(message, separators=(",", ":"))
        async with self._lock:
            targets = list(self._connections)

        dead: list[WebSocket] = []
        for connection in targets:
            try:
                await connection.send_text(payload)
            except Exception:  # noqa: BLE001 - any send failure means it is gone
                dead.append(connection)

        if dead:
            async with self._lock:
                for connection in dead:
                    self._connections.discard(connection)
            log.info("dropped %d dead socket(s)", len(dead))


manager = ConnectionManager()


def idle_snapshot(credentials_present: bool) -> SessionState:
    """What we send when no data source is running (LIVE_MODE off, or no creds)."""
    return SessionState(
        phase=PHASE,
        mode="idle",
        server_time=_utc_now_iso(),
        credentials_present=credentials_present,
    )


async def websocket_endpoint(
    websocket: WebSocket,
    snapshot_provider: SnapshotProvider,
) -> None:
    await manager.connect(websocket)
    try:
        while True:
            try:
                state = snapshot_provider()
            except Exception:  # noqa: BLE001
                # A snapshot failure must not close the socket: the next tick
                # may well succeed, and a dead socket looks like a dead backend.
                log.exception("snapshot failed; sending an idle frame")
                state = idle_snapshot(False)

            await websocket.send_text(state.model_dump_json())
            await asyncio.sleep(PUSH_INTERVAL_SECONDS)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.exception("websocket loop failed")
    finally:
        await manager.disconnect(websocket)
        with contextlib.suppress(Exception):
            await websocket.close()
