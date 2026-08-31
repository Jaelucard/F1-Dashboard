"""The single WebSocket fan-out to connected browsers.

Phase 0 only proves the socket opens and stays open. Phase 2 replaces the
placeholder payload with a normalised ``SessionState`` snapshot, but the
envelope shape and the broadcast machinery below stay as they are: every mode
(live, replay, historical) pushes through this one path, so the browser cannot
tell them apart.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger(__name__)

# How often we push, even when nothing changed. A steady beat is what lets the
# frontend's data-age counter distinguish "feed is quiet" from "backend died".
PUSH_INTERVAL_SECONDS = 1.0


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

        Serialise once rather than per-connection: with 20+ drivers the snapshot
        is the biggest thing we send, and re-encoding it per browser is waste.
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


def placeholder_snapshot(live_mode: bool, credentials_present: bool) -> dict[str, Any]:
    """The Phase 0 stand-in for a real SessionState snapshot."""
    return {
        "type": "snapshot",
        "server_time": _utc_now_iso(),
        "phase": 0,
        "mode": "live" if live_mode else "idle",
        "credentials_present": credentials_present,
        "session": None,
        "drivers": [],
    }


async def websocket_endpoint(
    websocket: WebSocket, live_mode: bool, credentials_present: bool
) -> None:
    await manager.connect(websocket)
    try:
        while True:
            await websocket.send_text(
                json.dumps(placeholder_snapshot(live_mode, credentials_present))
            )
            await asyncio.sleep(PUSH_INTERVAL_SECONDS)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.exception("websocket loop failed")
    finally:
        await manager.disconnect(websocket)
        with contextlib.suppress(Exception):
            await websocket.close()
