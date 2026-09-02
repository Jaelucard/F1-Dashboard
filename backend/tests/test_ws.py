"""ws.py: access control, one snapshot per tick, and slow clients dropped."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from app import PHASE, ws
from app.config import get_settings
from app.main import app
from app.models import SessionState

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def idle_state() -> SessionState:
    return ws.idle_snapshot(False)


def env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


# -- policy: origin, auth, limits (through the real app) ------------------------


def test_a_browser_from_an_unknown_origin_is_refused_before_accept() -> None:
    with TestClient(app) as client:
        with pytest.raises(WebSocketDenialResponse) as excinfo:
            with client.websocket_connect("/ws", headers={"Origin": "http://evil.example"}):
                pass
        assert excinfo.value.status_code == 403
        assert ws.manager.stats()["rejected"]["origin"] >= 1


def test_the_dev_server_origin_is_allowed() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws", headers={"Origin": "http://localhost:5173"}) as socket:
            assert socket.receive_json()["type"] == "snapshot"


def test_origin_comparison_ignores_case_and_trailing_slash() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws", headers={"Origin": "HTTP://LOCALHOST:5173/"}) as socket:
            assert socket.receive_json()["type"] == "snapshot"


def test_no_origin_is_allowed_by_default_but_can_be_required(monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as socket:  # curl / websocat send no Origin
            assert socket.receive_json()["type"] == "snapshot"

    env(monkeypatch, WS_REQUIRE_ORIGIN="true")
    with TestClient(app) as client:
        with pytest.raises(WebSocketDenialResponse):
            with client.websocket_connect("/ws"):
                pass


def test_a_shared_token_gates_the_socket_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    env(monkeypatch, WS_AUTH_TOKEN="test-ws-token-placeholder")
    with TestClient(app) as client:
        with pytest.raises(WebSocketDenialResponse):
            with client.websocket_connect("/ws"):
                pass
        with pytest.raises(WebSocketDenialResponse):
            with client.websocket_connect("/ws?token=wrong"):
                pass
        with client.websocket_connect("/ws?token=test-ws-token-placeholder") as socket:
            assert socket.receive_json()["type"] == "snapshot"
        with client.websocket_connect("/ws", headers={"Authorization": "Bearer test-ws-token-placeholder"}) as socket:
            assert socket.receive_json()["type"] == "snapshot"
        assert ws.manager.stats()["rejected"]["auth"] >= 2


def test_the_token_never_appears_in_health() -> None:
    with TestClient(app) as client:
        body = json.dumps(client.get("/health").json())
        assert "test-ws-token-placeholder" not in body


def test_client_limit_refuses_the_extra_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    env(monkeypatch, WS_MAX_CLIENTS="1")
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as first:
            assert first.receive_json()["type"] == "snapshot"
            with pytest.raises(WebSocketDenialResponse):
                with client.websocket_connect("/ws"):
                    pass
            assert ws.manager.stats()["rejected"]["limit"] >= 1
        # Once the first leaves, a new one is welcome.
        with client.websocket_connect("/ws") as again:
            assert again.receive_json()["type"] == "snapshot"


# -- broadcaster (unit, with fake sockets) -----------------------------------


class FakeSocket:
    """Just enough of starlette's WebSocket for ConnectionManager.serve."""

    def __init__(self, *, send_delay: float = 0.0, block: asyncio.Event | None = None) -> None:
        self.sent: list[str] = []
        self.accepted = False
        self.closed: tuple[int, str] | None = None
        self.headers: dict[str, str] = {"origin": "http://localhost:5173"}
        self.query_params: dict[str, str] = {}
        self._send_delay = send_delay
        self._block = block
        self._gone = asyncio.Event()

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)
        self._gone.set()

    async def send_text(self, text: str) -> None:
        if self._block is not None:
            await self._block.wait()
        if self._send_delay:
            await asyncio.sleep(self._send_delay)
        self.sent.append(text)

    async def receive(self) -> dict[str, Any]:
        await self._gone.wait()
        return {"type": "websocket.disconnect", "code": 1000}

    def leave(self) -> None:
        self._gone.set()


def make_manager(**kwargs: Any) -> ws.ConnectionManager:
    policy = ws.Policy(
        allowed_origins=frozenset({"http://localhost:5173"}),
        require_origin=False,
        auth_token="",
        max_clients=kwargs.pop("max_clients", 16),
        send_timeout=kwargs.pop("send_timeout", 0.2),
        push_interval=kwargs.pop("push_interval", 0.02),
    )
    return ws.ConnectionManager(policy_provider=lambda: policy)


async def run_clients(manager: ws.ConnectionManager, provider: Any, *sockets: FakeSocket) -> list[asyncio.Task[None]]:
    tasks = [asyncio.create_task(manager.serve(socket, provider)) for socket in sockets]
    await asyncio.sleep(0)
    return tasks


async def test_one_snapshot_is_built_per_tick_regardless_of_client_count() -> None:
    calls = {"n": 0}

    def provider() -> SessionState:
        calls["n"] += 1
        return idle_state()

    manager = make_manager()
    sockets = [FakeSocket() for _ in range(5)]
    tasks = await run_clients(manager, provider, *sockets)
    await asyncio.sleep(0.2)
    for socket in sockets:
        socket.leave()
    await asyncio.gather(*tasks)

    assert calls["n"] >= 3
    assert calls["n"] == manager.stats()["frames_built"]
    for socket in sockets:
        assert len(socket.sent) >= 3
    # Five clients, one build per tick: nowhere near 5x the frames.
    assert calls["n"] <= max(len(s.sent) for s in sockets) + 2


async def test_a_slow_client_is_dropped_and_the_rest_keep_flowing() -> None:
    manager = make_manager(send_timeout=0.05)
    stuck = FakeSocket(block=asyncio.Event())  # never releases
    healthy = FakeSocket()
    tasks = await run_clients(manager, idle_state, stuck, healthy)
    await asyncio.sleep(0.3)
    healthy.leave()
    await asyncio.gather(*tasks)

    assert stuck.closed is not None
    assert stuck.sent == []
    assert len(healthy.sent) >= 3
    assert manager.stats()["slow_clients_dropped"] == 1
    assert manager.count == 0


async def test_slow_clients_get_the_latest_frame_not_a_backlog() -> None:
    """Latest-wins: while a client is busy, frames are replaced, never queued."""
    manager = make_manager(send_timeout=5.0, push_interval=0.01)
    gate = asyncio.Event()
    socket = FakeSocket(block=gate)
    counter = {"n": 0}

    def provider() -> SessionState:
        counter["n"] += 1
        state = idle_state()
        state.server_time = f"frame-{counter['n']}"
        return state

    tasks = await run_clients(manager, provider, socket)
    await asyncio.sleep(0.15)  # many ticks while the send is blocked
    built_while_blocked = counter["n"]
    gate.set()
    await asyncio.sleep(0.05)
    socket.leave()
    await asyncio.gather(*tasks)

    assert built_while_blocked >= 5
    # The blocked send completes with the first frame, then the client gets
    # only the newest one - not the five-plus that were built meanwhile.
    delivered = [json.loads(s)["server_time"] for s in socket.sent]
    assert len(delivered) <= built_while_blocked - 3
    assert manager.stats()["frames_skipped"] >= 3


async def test_a_snapshot_failure_resends_the_last_good_state_marked_degraded() -> None:
    good = idle_state()
    good.session_status = "Started"
    state = {"fail": False}

    def provider() -> SessionState:
        if state["fail"]:
            raise RuntimeError("boom")
        return good

    manager = make_manager(push_interval=0.02)
    socket = FakeSocket()
    tasks = await run_clients(manager, provider, socket)
    await asyncio.sleep(0.05)
    state["fail"] = True
    await asyncio.sleep(0.08)
    socket.leave()
    await asyncio.gather(*tasks)

    frames = [json.loads(s) for s in socket.sent]
    assert frames[0]["degraded"] is False
    degraded = [f for f in frames if f["degraded"]]
    assert degraded, "the failure must be visible, not hidden behind an idle frame"
    assert all(f["session_status"] == "Started" for f in degraded), "last good state preserved"
    assert all("RuntimeError" in (f["degraded_reason"] or "") for f in degraded)
    assert manager.stats()["snapshot_failures"] >= 1


async def test_a_snapshot_failure_with_no_prior_state_is_an_explicit_degraded_frame() -> None:
    def provider() -> SessionState:
        raise RuntimeError("boom")

    manager = make_manager(push_interval=0.02)
    socket = FakeSocket()
    tasks = await run_clients(manager, provider, socket)
    await asyncio.sleep(0.05)
    socket.leave()
    await asyncio.gather(*tasks)

    frame = json.loads(socket.sent[0])
    assert frame["mode"] == "idle" and frame["degraded"] is True
    assert frame["phase"] == PHASE


async def test_the_broadcaster_stops_when_the_last_client_leaves() -> None:
    calls = {"n": 0}

    def provider() -> SessionState:
        calls["n"] += 1
        return idle_state()

    manager = make_manager(push_interval=0.01)
    socket = FakeSocket()
    tasks = await run_clients(manager, provider, socket)
    await asyncio.sleep(0.05)
    socket.leave()
    await asyncio.gather(*tasks)
    settled = calls["n"]
    await asyncio.sleep(0.05)
    assert calls["n"] == settled, "no snapshots built for nobody"
    assert manager.stats()["broadcasting"] is False
