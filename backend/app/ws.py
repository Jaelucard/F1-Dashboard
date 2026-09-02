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

How it is shaped
----------------
* **One broadcaster, one snapshot per tick.** A single task builds and
  serialises the frame once per interval and hands the same string to every
  client. Twenty tabs cost one snapshot, not twenty.
* **Latest wins, per client.** Each client has a one-slot mailbox. If it is
  still busy sending the previous frame when the next arrives, the old frame
  is replaced, never queued, so a stalled tab cannot grow an unbounded backlog.
* **Slow clients are dropped.** A send that does not complete within the
  timeout closes that socket; the others are unaffected.
* **Access control before accept.** Origin is checked (a browser always sends
  it), an optional shared token is checked, and a client limit is enforced.
  Refusals happen before the handshake completes, so they are a 403.
* **A failed snapshot is a degraded frame, not a fake idle one.** The last
  good state is re-sent with ``degraded=true`` and the reason, so the browser
  keeps showing real data and the operator can see something is wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import WebSocket
from fastapi.responses import PlainTextResponse

from app import PHASE
from app.config import get_settings
from app.models import SessionState

log = logging.getLogger(__name__)

PUSH_INTERVAL_SECONDS = 1.0

SnapshotProvider = Callable[[], SessionState]

CLOSE_ORIGIN = 1008      # policy violation
CLOSE_LIMIT = 1013       # try again later
CLOSE_UNAUTHORIZED = 4401
CLOSE_SLOW = 4408


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Policy:
    allowed_origins: frozenset[str]
    require_origin: bool
    auth_token: str
    max_clients: int
    send_timeout: float
    push_interval: float


def policy_from_settings() -> Policy:
    settings = get_settings()
    return Policy(
        allowed_origins=settings.allowed_origins,
        require_origin=settings.ws_require_origin,
        auth_token=settings.ws_auth_token.get_secret_value().strip(),
        max_clients=settings.ws_max_clients,
        send_timeout=settings.ws_send_timeout_seconds,
        push_interval=settings.ws_push_interval_seconds,
    )


def _header(websocket: Any, name: str) -> str | None:
    headers = getattr(websocket, "headers", None)
    if headers is None:
        return None
    value = headers.get(name)
    return value if isinstance(value, str) else None


def _query(websocket: Any, name: str) -> str | None:
    params = getattr(websocket, "query_params", None)
    if params is None:
        return None
    value = params.get(name)
    return value if isinstance(value, str) else None


class _Client:
    """One browser: its socket and its one-slot mailbox."""

    __slots__ = ("websocket", "latest", "wake", "skipped")

    def __init__(self, websocket: Any) -> None:
        self.websocket = websocket
        self.latest: str | None = None
        self.wake = asyncio.Event()
        self.skipped = 0

    def offer(self, frame: str) -> None:
        if self.latest is not None:
            # The sender has not taken the previous frame yet: replace it.
            # Latest wins; nothing queues behind a slow send.
            self.skipped += 1
        self.latest = frame
        self.wake.set()

    def take(self) -> str | None:
        frame, self.latest = self.latest, None
        self.wake.clear()
        return frame


class ConnectionManager:
    """Tracks open browser sockets and broadcasts one frame per tick to all."""

    def __init__(self, *, policy_provider: Callable[[], Policy] = policy_from_settings) -> None:
        self._policy_provider = policy_provider
        self._clients: set[_Client] = set()
        self._broadcast_task: asyncio.Task[None] | None = None
        self._provider: SnapshotProvider | None = None
        self._last_good: SessionState | None = None
        self._last_frame: str | None = None
        self._last_frame_at: float = 0.0

        self.frames_built = 0
        self.snapshot_failures = 0
        self.slow_clients_dropped = 0
        self.rejected: dict[str, int] = {"origin": 0, "auth": 0, "limit": 0}
        self._skipped_total = 0

    # -- introspection ------------------------------------------------------

    @property
    def count(self) -> int:
        return len(self._clients)

    def stats(self) -> dict[str, Any]:
        return {
            "clients": len(self._clients),
            "broadcasting": self._broadcast_task is not None and not self._broadcast_task.done(),
            "frames_built": self.frames_built,
            "frames_skipped": sum(c.skipped for c in self._clients) + self._skipped_total,
            "snapshot_failures": self.snapshot_failures,
            "slow_clients_dropped": self.slow_clients_dropped,
            "rejected": dict(self.rejected),
        }

    # -- policy ---------------------------------------------------------------

    def refuse(self, websocket: Any, policy: Policy) -> tuple[int, str] | None:
        """Why this handshake must be refused, or None to accept it."""
        origin = _header(websocket, "origin")
        if origin is None:
            if policy.require_origin:
                self.rejected["origin"] += 1
                return CLOSE_ORIGIN, "origin required"
        elif origin.strip().rstrip("/").lower() not in policy.allowed_origins:
            self.rejected["origin"] += 1
            log.warning("refused websocket from origin %r", origin[:200])
            return CLOSE_ORIGIN, "origin not allowed"

        if policy.auth_token:
            presented = _query(websocket, "token")
            if presented is None:
                auth = _header(websocket, "authorization") or ""
                if auth.lower().startswith("bearer "):
                    presented = auth[7:].strip()
            if presented is None or not hmac.compare_digest(presented, policy.auth_token):
                self.rejected["auth"] += 1
                return CLOSE_UNAUTHORIZED, "unauthorized"

        if len(self._clients) >= policy.max_clients:
            self.rejected["limit"] += 1
            log.warning("refused websocket: %d client(s) already connected", len(self._clients))
            return CLOSE_LIMIT, "too many clients"
        return None

    # -- serving ----------------------------------------------------------------

    async def serve(self, websocket: Any, provider: SnapshotProvider) -> None:
        """Handle one browser for the life of its socket."""
        policy = self._policy_provider()
        refusal = self.refuse(websocket, policy)
        if refusal is not None:
            await self._deny(websocket, *refusal)
            return

        await websocket.accept()
        client = _Client(websocket)
        self._clients.add(client)
        self._provider = provider
        log.info("browser connected (%d open)", len(self._clients))

        # A fresh frame if one exists, so the first paint does not wait a tick.
        if self._last_frame is not None and time.monotonic() - self._last_frame_at < policy.push_interval:
            client.offer(self._last_frame)
        self._ensure_broadcasting(policy)

        sender = asyncio.create_task(self._send_loop(client, policy))
        receiver = asyncio.create_task(self._receive_loop(client))
        try:
            done, pending = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
            for task in done:
                with contextlib.suppress(BaseException):
                    task.result()
        finally:
            self._clients.discard(client)
            self._skipped_total += client.skipped
            with contextlib.suppress(Exception):
                await websocket.close()
            log.info("browser disconnected (%d open)", len(self._clients))

    @staticmethod
    async def _deny(websocket: Any, code: int, reason: str) -> None:
        """Refuse before the handshake: an HTTP 403/503, not a half-open socket."""
        status = 503 if code == CLOSE_LIMIT else 403
        send_denial = getattr(websocket, "send_denial_response", None)
        with contextlib.suppress(Exception):
            if send_denial is not None:
                await send_denial(PlainTextResponse(reason, status_code=status))
            else:
                await websocket.close(code=code, reason=reason)

    async def _send_loop(self, client: _Client, policy: Policy) -> None:
        while True:
            await client.wake.wait()
            frame = client.take()
            if frame is None:
                continue
            try:
                await asyncio.wait_for(client.websocket.send_text(frame), timeout=policy.send_timeout)
            except asyncio.TimeoutError:
                self.slow_clients_dropped += 1
                log.warning("dropping a slow browser: send took longer than %.1fs", policy.send_timeout)
                with contextlib.suppress(Exception):
                    await client.websocket.close(code=CLOSE_SLOW, reason="too slow")
                return
            except Exception:  # noqa: BLE001 - any send failure means it is gone
                return

    @staticmethod
    async def _receive_loop(client: _Client) -> None:
        """Consume (and ignore) client frames so a disconnect is noticed promptly."""
        while True:
            message = await client.websocket.receive()
            if isinstance(message, dict) and message.get("type") == "websocket.disconnect":
                return

    # -- broadcasting -----------------------------------------------------------

    def _ensure_broadcasting(self, policy: Policy) -> None:
        if self._broadcast_task is None or self._broadcast_task.done():
            self._broadcast_task = asyncio.create_task(self._broadcast_loop(policy))

    async def _broadcast_loop(self, policy: Policy) -> None:
        try:
            while self._clients:
                frame = self.build_frame()
                for client in list(self._clients):
                    client.offer(frame)
                await asyncio.sleep(policy.push_interval)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("broadcast loop failed; it restarts with the next client")
        finally:
            self._broadcast_task = None

    def build_frame(self) -> str:
        """One serialised SessionState: fresh if possible, degraded if not."""
        provider = self._provider
        state: SessionState
        try:
            if provider is None:
                raise RuntimeError("no snapshot provider")
            state = provider()
            self._last_good = state
        except Exception as exc:  # noqa: BLE001
            self.snapshot_failures += 1
            log.exception("snapshot failed; re-sending the last good state as degraded")
            base = self._last_good
            state = base.model_copy() if base is not None else idle_snapshot(False)
            state.server_time = _utc_now_iso()
            state.degraded = True
            state.degraded_reason = f"snapshot failed: {type(exc).__name__}: {exc}"[:300]
        else:
            self.frames_built += 1

        frame = state.model_dump_json()
        self._last_frame = frame
        self._last_frame_at = time.monotonic()
        return frame


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
    await manager.serve(websocket, snapshot_provider)
