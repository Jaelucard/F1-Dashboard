"""/replay control API: availability, auth, transitions, and no paths in responses."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from tests.test_replay import KEY, session_messages, write_recording

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def recording() -> None:
    write_recording(get_settings().recordings_dir, KEY, session_messages(drivers=2))


def wait_for(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


# -- listing and idle -----------------------------------------------------------


def test_sessions_lists_recordings_without_paths() -> None:
    recording()
    with TestClient(app) as client:
        body = client.get("/replay/sessions").json()
    assert [s["session_key"] for s in body["sessions"]] == [KEY]
    assert body["sessions"][0]["session_name"] == "Practice 1"
    assert str(get_settings().recordings_dir) not in json.dumps(body)


def test_replay_is_idle_until_loaded() -> None:
    with TestClient(app) as client:
        assert client.get("/replay").json() == {"state": "idle"}
        assert client.get("/health").json()["replay"] is None


# -- transitions ----------------------------------------------------------------


def test_load_play_pause_speed_seek_unload() -> None:
    recording()
    with TestClient(app) as client:
        loaded = client.post("/replay/load", json={"session_key": KEY})
        assert loaded.status_code == 200, loaded.text
        assert loaded.json()["state"] == "loaded"

        with client.websocket_connect("/ws") as socket:
            frame = socket.receive_json()
            assert frame["mode"] == "replay" and frame["replay"]["session_key"] == KEY

        assert client.post("/replay/speed", json={"speed": 50}).json()["speed"] == 50
        assert client.post("/replay/play").json()["state"] == "playing"
        assert wait_for(lambda: client.get("/replay").json()["state"] == "finished")
        assert client.get("/replay").json()["progress"] == 1.0

        seek = client.post("/replay/seek", json={"fraction": 0.5})
        assert seek.status_code == 200
        assert wait_for(lambda: client.get("/replay").json()["state"] == "paused")
        assert 0.4 < client.get("/replay").json()["progress"] < 0.6

        assert client.post("/replay/pause").json()["state"] == "paused"
        assert client.post("/replay/unload").json() == {"state": "idle"}
        assert client.get("/replay").json() == {"state": "idle"}


def test_unknown_session_is_404_without_a_path() -> None:
    with TestClient(app) as client:
        response = client.post("/replay/load", json={"session_key": 4242})
    assert response.status_code == 404
    assert str(get_settings().recordings_dir) not in response.text


@pytest.mark.parametrize(
    ("path", "body"),
    [("/replay/seek", {}), ("/replay/seek", {"fraction": 0.5, "position": "2026-09-04T11:30:00+00:00"}),
     ("/replay/seek", {"fraction": 2}), ("/replay/speed", {"speed": 0}), ("/replay/load", {"session_key": -1})],
)
def test_invalid_bodies_are_422(path: str, body: dict[str, Any]) -> None:
    recording()
    with TestClient(app) as client:
        if path != "/replay/load":
            assert client.post("/replay/load", json={"session_key": KEY}).status_code == 200
        assert client.post(path, json=body).status_code == 422


def test_controls_without_a_loaded_recording_are_409() -> None:
    with TestClient(app) as client:
        assert client.post("/replay/play").status_code == 409
        assert client.post("/replay/seek", json={"fraction": 0.5}).status_code == 409


def test_health_carries_replay_state_and_never_a_path() -> None:
    recording()
    with TestClient(app) as client:
        client.post("/replay/load", json={"session_key": KEY})
        body = client.get("/health").json()
    assert body["replay"]["state"] == "loaded" and body["replay"]["session_key"] == KEY
    dumped = json.dumps(body)
    assert str(get_settings().recordings_dir) not in dumped
    assert "recordings_dir" not in dumped


# -- availability ---------------------------------------------------------------


def test_replay_is_refused_while_a_recorder_is_running() -> None:
    recording()
    with TestClient(app) as client:
        app.state.recorder = object()
        try:
            response = client.post("/replay/load", json={"session_key": KEY})
        finally:
            app.state.recorder = None
    assert response.status_code == 409
    assert "live recording" in response.json()["detail"]


def test_replay_is_refused_in_demo_mode() -> None:
    recording()
    with TestClient(app) as client:
        app.state.demo = True
        try:
            response = client.post("/replay/load", json={"session_key": KEY})
        finally:
            app.state.demo = False
    assert response.status_code == 409
    assert "demo" in response.json()["detail"]


def test_loading_replaces_a_running_replay() -> None:
    recording()
    write_recording(get_settings().recordings_dir, KEY + 1, session_messages(key=KEY + 1, drivers=1))
    with TestClient(app) as client:
        client.post("/replay/load", json={"session_key": KEY})
        client.post("/replay/play")
        second = client.post("/replay/load", json={"session_key": KEY + 1}).json()
        assert second["session_key"] == KEY + 1 and second["state"] == "loaded"


# -- auth -----------------------------------------------------------------------


def test_posts_require_the_shared_token_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    recording()
    env(monkeypatch, WS_AUTH_TOKEN="test-ws-token-placeholder")
    with TestClient(app) as client:
        assert client.get("/replay/sessions").status_code == 200, "GET stays open, like /health"
        assert client.post("/replay/load", json={"session_key": KEY}).status_code == 401
        assert client.post("/replay/load?token=wrong", json={"session_key": KEY}).status_code == 401
        ok = client.post("/replay/load", json={"session_key": KEY},
                         headers={"Authorization": "Bearer test-ws-token-placeholder"})
        assert ok.status_code == 200
        assert client.post("/replay/pause?token=test-ws-token-placeholder").status_code == 200
        assert "test-ws-token-placeholder" not in client.get("/health").text
    get_settings.cache_clear()
