"""Health is liveness; readiness is the honest answer. Feed state is neither."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.feed import compute_feed, evaluate_health
from app.main import app

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

NOW = datetime(2026, 9, 4, 11, 46, 5, tzinfo=timezone.utc)


def recorder_health(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "connected": True,
        "auth_state": "authenticated",
        "last_message_at": (NOW - timedelta(seconds=1)).isoformat(timespec="milliseconds"),
        "last_error": None,
        "recording_ok": True,
        "write_failures": 0,
        "fanout_dropped": 0,
        "may_be_incomplete": False,
        "disk_low": False,
        "disconnects_after_data": 0,
        "messages_possibly_lost": 0,
    }
    base.update(overrides)
    return base


# -- feed state ---------------------------------------------------------------


def test_feed_is_live_only_when_connected_authenticated_and_fresh() -> None:
    feed = compute_feed(recorder_health(), stale_after=15.0, now=NOW)
    assert feed.state == "live"
    assert feed.mqtt_connected and feed.authenticated
    assert feed.data_age_seconds == pytest.approx(1.0)


def test_feed_is_stale_when_data_stops_even_though_mqtt_is_up() -> None:
    old = (NOW - timedelta(seconds=40)).isoformat(timespec="milliseconds")
    feed = compute_feed(recorder_health(last_message_at=old), stale_after=15.0, now=NOW)
    assert feed.state == "stale"
    assert feed.mqtt_connected is True
    assert feed.data_age_seconds == pytest.approx(40.0)


def test_feed_is_connected_but_not_live_before_the_first_message() -> None:
    feed = compute_feed(recorder_health(last_message_at=None), stale_after=15.0, now=NOW)
    assert feed.state == "connected"
    assert feed.data_age_seconds is None


def test_feed_is_connecting_when_mqtt_is_down_and_nothing_has_arrived() -> None:
    feed = compute_feed(recorder_health(connected=False, auth_state="authenticating", last_message_at=None),
                        stale_after=15.0, now=NOW)
    assert feed.state == "connecting"
    assert feed.authenticated is False


def test_feed_is_stale_when_mqtt_drops_after_data() -> None:
    feed = compute_feed(recorder_health(connected=False), stale_after=15.0, now=NOW)
    assert feed.state == "stale"
    assert feed.mqtt_connected is False


def test_feed_reports_auth_failure_above_everything_else() -> None:
    feed = compute_feed(recorder_health(connected=False, auth_state="failed",
                                        last_error="authentication failed (credentials): HTTP 401"),
                        stale_after=15.0, now=NOW)
    assert feed.state == "auth_failed"
    assert feed.last_error is not None and "credentials" in feed.last_error


def test_feed_is_offline_without_a_recorder() -> None:
    assert compute_feed(None, stale_after=15.0, now=NOW).state == "offline"


def test_feed_carries_recording_health() -> None:
    feed = compute_feed(recorder_health(recording_ok=False, last_error="recording failed: disk_full"),
                        stale_after=15.0, now=NOW)
    assert feed.recording_ok is False
    assert feed.state == "live", "data is arriving; the disk is a separate problem"


def test_feed_tolerates_a_malformed_timestamp() -> None:
    feed = compute_feed(recorder_health(last_message_at="not a time"), stale_after=15.0, now=NOW)
    assert feed.state == "connected" and feed.data_age_seconds is None


# -- health evaluation ----------------------------------------------------------


def test_idle_backend_is_ok_and_ready() -> None:
    status, ready, problems = evaluate_health(live_mode=False, demo_mode=False, credentials_present=False,
                                              recorder=None, feed=None, now=NOW)
    assert (status, ready, problems) == ("ok", True, [])


def test_demo_mode_is_ok_and_ready() -> None:
    status, ready, _ = evaluate_health(live_mode=False, demo_mode=True, credentials_present=False,
                                       recorder=None, feed=None, now=NOW)
    assert (status, ready) == ("ok", True)


def test_live_mode_without_credentials_is_an_error_and_not_ready() -> None:
    status, ready, problems = evaluate_health(live_mode=True, demo_mode=False, credentials_present=False,
                                              recorder=None, feed=None, now=NOW)
    assert status == "error" and ready is False
    assert any("credentials" in p for p in problems)


def test_live_mode_with_a_recorder_that_failed_to_start_is_an_error() -> None:
    status, ready, problems = evaluate_health(live_mode=True, demo_mode=False, credentials_present=True,
                                              recorder=None, feed=None, now=NOW)
    assert status == "error" and ready is False
    assert any("recorder" in p for p in problems)


def test_connecting_is_degraded_and_not_ready() -> None:
    health = recorder_health(connected=False, auth_state="authenticating", last_message_at=None)
    feed = compute_feed(health, stale_after=15.0, now=NOW)
    status, ready, problems = evaluate_health(live_mode=True, demo_mode=False, credentials_present=True,
                                              recorder=health, feed=feed, now=NOW)
    assert status == "degraded" and ready is False
    assert any("MQTT" in p for p in problems)


def test_auth_failure_is_an_error() -> None:
    health = recorder_health(connected=False, auth_state="failed", last_error="authentication failed (credentials)")
    feed = compute_feed(health, stale_after=15.0, now=NOW)
    status, ready, problems = evaluate_health(live_mode=True, demo_mode=False, credentials_present=True,
                                              recorder=health, feed=feed, now=NOW)
    assert status == "error" and ready is False
    assert any("authentication" in p for p in problems)


def test_recording_failure_is_an_error_even_while_data_flows() -> None:
    health = recorder_health(recording_ok=False, write_failures=12, last_error="recording failed: disk_full")
    feed = compute_feed(health, stale_after=15.0, now=NOW)
    status, ready, problems = evaluate_health(live_mode=True, demo_mode=False, credentials_present=True,
                                              recorder=health, feed=feed, now=NOW)
    assert status == "error" and ready is False
    assert any("recording" in p for p in problems)


def test_stale_feed_low_disk_and_drops_are_degraded_but_ready() -> None:
    old = (NOW - timedelta(seconds=90)).isoformat(timespec="milliseconds")
    health = recorder_health(last_message_at=old, disk_low=True, fanout_dropped=5, may_be_incomplete=True,
                             disconnects_after_data=1)
    feed = compute_feed(health, stale_after=15.0, now=NOW)
    status, ready, problems = evaluate_health(live_mode=True, demo_mode=False, credentials_present=True,
                                              recorder=health, feed=feed, now=NOW)
    assert status == "degraded" and ready is True
    joined = " ".join(problems)
    assert "stale" in joined and "disk" in joined and "dropped" in joined and "incomplete" in joined


def test_healthy_live_feed_is_ok_and_ready() -> None:
    health = recorder_health()
    feed = compute_feed(health, stale_after=15.0, now=NOW)
    assert evaluate_health(live_mode=True, demo_mode=False, credentials_present=True,
                           recorder=health, feed=feed, now=NOW) == ("ok", True, [])


# -- routes ---------------------------------------------------------------------


def test_health_route_reports_status_ready_and_problems() -> None:
    with TestClient(app) as client:
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["ready"] is True
        assert body["problems"] == []
        assert body["feed"] is None
        assert "websocket" in body and body["websocket"]["clients"] == 0
        assert "feed_stale_seconds" in body


def test_ready_route_is_503_when_live_mode_has_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVE_MODE", "true")
    get_settings.cache_clear()
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["status"] == "error"
        assert health["ready"] is False
        assert health["recorder"] is None
        ready = client.get("/ready")
        assert ready.status_code == 503
        assert ready.json()["ready"] is False
        assert ready.json()["problems"]
    get_settings.cache_clear()


def test_ready_route_is_200_when_idle_by_design() -> None:
    with TestClient(app) as client:
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json()["ready"] is True
