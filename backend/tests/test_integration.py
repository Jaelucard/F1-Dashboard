"""End to end without the network: MQTT callback -> recorder -> adapter -> snapshot -> WebSocket.

A fake paho client delivers messages into the real recorder, which writes them
to a temporary directory and fans them out to the real live adapter; the real
FastAPI app then serves the resulting SessionState over the real WebSocket
route. Nothing here needs credentials, a broker, or the internet.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.adapters.openf1_live import OpenF1LiveSource
from app.config import Settings, get_settings
from app.main import app
from app.recorder import RawRecorder
from scripts.sample_data import build_messages

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class FakeTokenProvider:
    username = "test-user@example.invalid"

    def __init__(self) -> None:
        self.current_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    def get_token(self, *, force_refresh: bool = False) -> str:
        return "test-token-placeholder"

    def seconds_until_refresh(self) -> float:
        return 3600.0

    def close(self) -> None:
        pass


class FakeClient:
    def __init__(self) -> None:
        self.on_connect: Any = None
        self.on_message: Any = None
        self.on_disconnect: Any = None
        self.on_subscribe: Any = None

    def username_pw_set(self, username: str, password: str) -> None:
        pass

    def connect(self, host: str, port: int, keepalive: int = 60) -> None:
        pass

    def loop_start(self) -> None:
        self.on_connect(self, None, {}, 0, None)

    def loop_stop(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def subscribe(self, topic: str, qos: int = 0) -> None:
        pass


class Message:
    def __init__(self, topic: str, payload: Any, *, raw: bytes | None = None) -> None:
        self.topic = topic
        self.payload = raw if raw is not None else json.dumps(payload).encode()


def wait_for(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def pipeline(tmp_path: Path) -> Any:
    """A started recorder + adapter wired exactly as main.lifespan wires them."""
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        openf1_username=SecretStr("test-user@example.invalid"),
        openf1_password=SecretStr("test-password-placeholder"),
        live_mode=True,
        recordings_dir=tmp_path,
        feed_stale_seconds=2.0,
    )
    client = FakeClient()
    recorder = RawRecorder(settings, FakeTokenProvider(), client_factory=lambda: client)  # type: ignore[arg-type]
    source = OpenF1LiveSource(credentials_present=True)
    recorder.add_subscriber(source.on_raw_message)
    recorder.start()
    assert wait_for(lambda: client.on_message is not None)
    yield settings, client, recorder, source
    recorder.stop(timeout=5)


def test_messages_flow_from_the_broker_callback_to_the_browser(pipeline: Any, tmp_path: Path) -> None:
    settings, client, recorder, source = pipeline

    for topic, payload in build_messages():
        client.on_message(client, None, Message(topic, payload))
    assert recorder.drain(timeout=5)

    # On disk first...
    assert (tmp_path / "9999" / "v1_laps.jsonl").exists()
    assert recorder.stats.messages_recorded == len(build_messages())
    # ...then in the adapter, on the fan-out thread, not paho's.
    assert source.stats()["messages_seen"] == len(build_messages())

    with TestClient(app) as http:
        app.state.recorder = recorder
        app.state.source = source
        app.state.demo = False
        with http.websocket_connect("/ws") as socket:
            frame = socket.receive_json()

        health = http.get("/health").json()

    assert frame["type"] == "snapshot" and frame["mode"] == "live"
    assert len(frame["drivers"]) == 22
    assert frame["drivers"][0]["name_acronym"] == "VER"
    assert frame["drivers"][0]["last_lap_duration"] == 79.681
    assert frame["session"]["session_key"] == 9999
    assert frame["degraded"] is False

    assert frame["feed"]["state"] == "live"
    assert frame["feed"]["mqtt_connected"] is True and frame["feed"]["authenticated"] is True
    assert frame["feed"]["data_age_seconds"] is not None and frame["feed"]["data_age_seconds"] < 5
    assert frame["recorder"]["connected"] is True
    assert frame["recorder"]["messages_recorded"] == len(build_messages())
    assert frame["recorder"]["recording_ok"] is True
    assert frame["adapter"]["active_session_key"] == 9999
    assert frame["adapter"]["quarantined_records"] == 0

    # The sample data's _id values are deliberately sparse (1000+n, 2000+n, ...)
    # so the gap heuristic honestly reports possible loss: degraded, still ready.
    assert health["status"] in ("ok", "degraded") and health["ready"] is True
    assert all("incomplete" in problem for problem in health["problems"])
    assert health["feed"]["state"] == "live"
    assert health["recorder"]["messages_recorded"] == len(build_messages())
    assert health["source"]["active_session_key"] == 9999


def test_a_session_change_and_a_broker_drop_are_visible_end_to_end(pipeline: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    settings, client, recorder, source = pipeline
    monkeypatch.setenv("FEED_STALE_SECONDS", "0.2")
    get_settings.cache_clear()

    for topic, payload in build_messages():
        client.on_message(client, None, Message(topic, payload))
    # FP2 starts: one car, nothing else.
    client.on_message(client, None, Message("v1/position", {"session_key": 10000, "driver_number": 7, "position": 1}))
    # A straggler from FP1 and a malformed record follow.
    client.on_message(client, None, Message("v1/laps", {"session_key": 9999, "driver_number": 100, "lap_number": 14, "lap_duration": 60.0}))
    client.on_message(client, None, Message("v1/laps", {"session_key": 10000, "lap_duration": "??"}))
    client.on_message(client, None, Message("v1/laps", None, raw=b"not json at all"))
    assert recorder.drain(timeout=5)

    with TestClient(app) as http:
        app.state.recorder = recorder
        app.state.source = source
        app.state.demo = False
        with http.websocket_connect("/ws") as socket:
            frame = socket.receive_json()
        assert [d["driver_number"] for d in frame["drivers"]] == [7]
        assert frame["session_best_lap"] is None, "FP1's laps must not leak into FP2"
        assert frame["adapter"]["active_session_key"] == 10000
        assert frame["adapter"]["late_session_messages"] == 1
        assert frame["adapter"]["quarantined_records"] == 2
        assert frame["recorder"]["messages_recorded"] == len(build_messages()) + 4

        # The broker drops us after data had flowed: not LIVE any more, and
        # the recording can no longer be called complete.
        recorder._on_disconnect(client, None, None, 7, None)  # noqa: SLF001
        time.sleep(1.2)  # past one push interval, so no cached frame is served
        with http.websocket_connect("/ws") as socket:
            frame = socket.receive_json()
        assert frame["feed"]["state"] == "stale"
        assert frame["feed"]["mqtt_connected"] is False
        assert frame["recorder"]["may_be_incomplete"] is True

        health = http.get("/health").json()
        assert health["status"] == "degraded"
        assert health["ready"] is False, "not connected: cannot serve live data right now"
        assert any("stale" in p for p in health["problems"])
        assert any("MQTT not connected" in p for p in health["problems"])
        assert health["recorder"]["disconnects_after_data"] == 1
        assert http.get("/ready").status_code == 503


def test_a_failing_adapter_degrades_the_frame_but_keeps_recording(pipeline: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings, client, recorder, source = pipeline

    class Exploding(OpenF1LiveSource):
        def snapshot(self, **kwargs: Any) -> Any:  # type: ignore[override]
            raise RuntimeError("adapter bug")

    broken = Exploding(credentials_present=True)
    recorder.add_subscriber(lambda message: (_ for _ in ()).throw(RuntimeError("subscriber bug")))

    for topic, payload in build_messages()[:10]:
        client.on_message(client, None, Message(topic, payload))
    assert recorder.drain(timeout=5)

    with TestClient(app) as http:
        app.state.recorder = recorder
        app.state.source = broken
        app.state.demo = False
        with http.websocket_connect("/ws") as socket:
            frame = socket.receive_json()

    assert frame["degraded"] is True
    assert "RuntimeError" in frame["degraded_reason"]
    assert recorder.stats.messages_recorded == 10
    assert recorder.stats.subscriber_errors == 10
    assert (tmp_path / "9999" / "v1_sessions.jsonl").exists()


def test_fan_out_runs_off_the_paho_thread(pipeline: Any) -> None:
    settings, client, recorder, source = pipeline
    threads: set[str] = set()
    recorder.add_subscriber(lambda message: threads.add(threading.current_thread().name))
    client.on_message(client, None, Message("v1/position", {"session_key": 1, "driver_number": 1, "position": 1}))
    assert recorder.drain(timeout=5)
    assert threads == {"openf1-fanout"}
