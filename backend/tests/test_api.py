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
        assert body["phase"] == 0
        assert set(body) >= {
            "status",
            "version",
            "live_mode",
            "credentials_present",
            "started_at",
            "browsers_connected",
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
        assert message["phase"] == 0
        assert message["drivers"] == []
        assert "server_time" in message
