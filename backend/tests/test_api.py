"""Phase 0 acceptance: the health route answers and the WebSocket pushes."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_health_route() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["phase"] == 2
        assert set(body) >= {
            "status",
            "version",
            "live_mode",
            "credentials_present",
            "started_at",
            "browsers_connected",
            "recorder",
        }


def test_health_never_leaks_credentials() -> None:
    with TestClient(app) as client:
        body = client.get("/health").json()
        # A boolean, never the value itself.
        assert isinstance(body["credentials_present"], bool)


def test_websocket_pushes_a_snapshot() -> None:
    with TestClient(app) as client, client.websocket_connect("/ws") as socket:
        message = socket.receive_json()
        assert message["type"] == "snapshot"
        assert message["phase"] == 2
        assert message["drivers"] == []
        assert "server_time" in message


def test_recorder_does_not_start_when_live_mode_is_off() -> None:
    """The suite runs with LIVE_MODE=false, so nothing should have connected."""
    with TestClient(app) as client:
        body = client.get("/health").json()
        assert body["live_mode"] is False
        assert body["recorder"] is None


def test_live_mode_without_credentials_does_not_crash_the_api(
    monkeypatch: object,
) -> None:
    """Misconfiguration must degrade to 'API up, recorder off', never a 500."""
    import pytest as _pytest

    from app.config import get_settings

    with _pytest.MonkeyPatch.context() as patch:
        patch.setenv("LIVE_MODE", "true")
        patch.setenv("OPENF1_USERNAME", "")
        patch.setenv("OPENF1_PASSWORD", "")
        get_settings.cache_clear()
        with TestClient(app) as client:
            response = client.get("/health")
            assert response.status_code == 200, "liveness must still answer"
            body = response.json()
            # ...but it must not pretend to be fine: live mode was asked for
            # and cannot be delivered.
            assert body["status"] == "error"
            assert body["ready"] is False
            assert any("credentials" in problem for problem in body["problems"])
            assert body["live_mode"] is True
            assert body["credentials_present"] is False
            assert body["recorder"] is None
            assert client.get("/ready").status_code == 503
    get_settings.cache_clear()
