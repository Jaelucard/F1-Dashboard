"""backfill.py against a fake OpenF1 REST API, never the network.

The hole being covered: connect after a session has started and the one-shot
v1/sessions and v1/drivers announcements are already gone, so the strip reads
NO SESSION, no outline loads and every row is a bare driver number.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from app.adapters.openf1_live import (
    TOPIC_DRIVERS,
    TOPIC_LAPS,
    TOPIC_SESSIONS,
    OpenF1LiveSource,
)
from app.backfill import (
    BackfillService,
    SessionBackfill,
    missing_identity_topics,
    needs_backfill,
)

SESSION_KEY = 11354

SESSIONS = [{
    "session_key": SESSION_KEY, "meeting_key": 1290, "circuit_key": 39,
    "session_name": "Practice 2", "session_type": "Practice",
    "circuit_short_name": "Monza", "country_name": "Italy", "location": "Monza",
    "year": 2026, "date_start": "2026-09-04T14:00:00+00:00",
    "date_end": "2026-09-04T15:00:00+00:00", "gmt_offset": "02:00:00",
}]

DRIVERS = [
    {"session_key": SESSION_KEY, "driver_number": 16, "name_acronym": "LEC",
     "full_name": "Charles Leclerc", "team_name": "Ferrari", "team_colour": "E8002D"},
    {"session_key": SESSION_KEY, "driver_number": 4, "name_acronym": "NOR",
     "full_name": "Lando Norris", "team_name": "McLaren", "team_colour": "F47600"},
]

LAPS = [
    {"session_key": SESSION_KEY, "driver_number": 16, "lap_number": 5,
     "lap_duration": 81.2, "duration_sector_1": 26.0, "duration_sector_2": 28.0,
     "duration_sector_3": 27.2, "is_pit_out_lap": False,
     "date_start": "2026-09-04T14:10:00+00:00"},
]

STINTS = [{"session_key": SESSION_KEY, "driver_number": 16, "stint_number": 1,
           "lap_start": 1, "lap_end": 8, "compound": "MEDIUM", "tyre_age_at_start": 0}]
PIT: list[dict[str, Any]] = []
RACE_CONTROL = [{"session_key": SESSION_KEY, "date": "2026-09-04T14:00:05+00:00",
                 "category": "Flag", "flag": "GREEN", "scope": "Track",
                 "message": "GREEN LIGHT - PIT EXIT OPEN"}]

BODIES = {
    "sessions": SESSIONS, "drivers": DRIVERS, "laps": LAPS,
    "stints": STINTS, "pit": PIT, "race_control": RACE_CONTROL,
}


def fake_api(seen: list[httpx.Request], *, status: dict[str, int] | None = None) -> httpx.AsyncClient:
    """Answers the six backfill paths. ``status`` forces a code for a path."""
    status = status or {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path.rsplit("/", 1)[-1]
        forced = status.get(path)
        if forced is not None and forced >= 400:
            # An authenticated request is refused; the unauthenticated retry
            # is allowed through, which is the supporter-tier behaviour.
            if request.headers.get("authorization"):
                return httpx.Response(forced, json={"detail": "restricted"})
        return httpx.Response(200, json=BODIES.get(path, []))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.openf1.org")


def timing_only_source() -> OpenF1LiveSource:
    """An adapter that joined mid-session: timing but no announcements."""
    source = OpenF1LiveSource()
    source.ingest(TOPIC_LAPS, {"session_key": SESSION_KEY, "driver_number": 16,
                               "lap_number": 4, "lap_duration": 82.0, "_id": 1})
    return source


# -- detection -------------------------------------------------------------------


def test_a_mid_session_connect_is_detected_as_needing_backfill() -> None:
    source = timing_only_source()
    assert source.active_session_key == SESSION_KEY
    assert missing_identity_topics(source) == [TOPIC_SESSIONS, TOPIC_DRIVERS]
    assert needs_backfill(source) is True
    # The symptoms the user actually sees.
    state = source.snapshot()
    assert state.session is None
    assert state.drivers[0].name_acronym is None


def test_a_session_that_announced_itself_needs_nothing() -> None:
    source = timing_only_source()
    source.ingest(TOPIC_SESSIONS, SESSIONS[0])
    for driver in DRIVERS:
        source.ingest(TOPIC_DRIVERS, driver)
    assert missing_identity_topics(source) == []
    assert needs_backfill(source) is False


def test_no_active_session_means_nothing_to_backfill() -> None:
    assert needs_backfill(OpenF1LiveSource()) is False


# -- fetching --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_populates_session_and_driver_identity() -> None:
    source = timing_only_source()
    seen: list[httpx.Request] = []
    async with fake_api(seen) as client:
        result = await SessionBackfill(
            source, client, api_base="https://api.openf1.org/v1", token_getter=lambda: "tok"
        ).run(SESSION_KEY)

    assert result.ok, result.errors
    assert result.rows[TOPIC_SESSIONS] == 1
    assert result.rows[TOPIC_DRIVERS] == 2

    state = source.snapshot()
    assert state.session is not None
    assert state.session.session_name == "Practice 2"
    assert state.session.circuit_key == 39, "the track map needs this to load an outline"
    assert state.session.circuit_short_name == "Monza"

    by_number = {d.driver_number: d for d in state.drivers}
    assert by_number[16].name_acronym == "LEC"
    assert by_number[16].team_colour == "E8002D"
    assert by_number[4].name_acronym == "NOR"
    # Laps and stints came too, so the row is not just a name.
    assert by_number[16].best_lap_duration == 81.2
    assert by_number[16].compound == "MEDIUM"


@pytest.mark.asyncio
async def test_backfill_asks_only_for_the_active_session_and_never_for_telemetry() -> None:
    seen: list[httpx.Request] = []
    async with fake_api(seen) as client:
        await SessionBackfill(
            timing_only_source(), client, api_base="https://api.openf1.org/v1"
        ).run(SESSION_KEY)

    paths = [r.url.path.rsplit("/", 1)[-1] for r in seen]
    assert paths == ["sessions", "drivers", "laps", "stints", "pit", "race_control"]
    # car_data must be windowed and location is huge; the live stream covers both.
    assert "car_data" not in paths and "location" not in paths
    for request in seen:
        assert parse_qs(request.url.query.decode())["session_key"] == [str(SESSION_KEY)]


@pytest.mark.asyncio
async def test_backfilled_rows_are_counted_apart_from_mqtt_messages() -> None:
    source = timing_only_source()
    seen_mqtt = source.snapshot().adapter.messages_seen
    async with fake_api([]) as client:
        await SessionBackfill(source, client, api_base="https://api.openf1.org/v1").run(SESSION_KEY)

    info = source.snapshot().adapter
    assert info.messages_seen == seen_mqtt, "REST rows must not inflate the wire count"
    assert info.backfilled_records == len(SESSIONS) + len(DRIVERS) + len(LAPS) + len(STINTS) + len(RACE_CONTROL)


@pytest.mark.asyncio
async def test_a_403_retries_once_without_the_token_then_succeeds() -> None:
    source = timing_only_source()
    seen: list[httpx.Request] = []
    async with fake_api(seen, status={"drivers": 403}) as client:
        result = await SessionBackfill(
            source, client, api_base="https://api.openf1.org/v1", token_getter=lambda: "tok"
        ).run(SESSION_KEY)

    drivers_requests = [r for r in seen if r.url.path.endswith("/drivers")]
    assert len(drivers_requests) == 2, "one with the token, one without"
    assert drivers_requests[0].headers.get("authorization") == "Bearer tok"
    assert drivers_requests[1].headers.get("authorization") is None
    assert result.ok and result.rows[TOPIC_DRIVERS] == 2


@pytest.mark.asyncio
async def test_a_401_that_survives_the_retry_is_reported_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/drivers"):
            return httpx.Response(401, json={"detail": "restricted"})
        return httpx.Response(200, json=BODIES.get(request.url.path.rsplit("/", 1)[-1], []))

    source = timing_only_source()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SessionBackfill(
            source, client, api_base="https://api.openf1.org/v1", token_getter=lambda: "tok"
        ).run(SESSION_KEY)

    assert result.errors == {"drivers": "HTTP 401"}
    # The session still came through, so the strip and the map are fixed even
    # though the names are not.
    assert source.snapshot().session is not None


@pytest.mark.asyncio
async def test_a_detail_object_instead_of_rows_is_an_error_not_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"detail": "No results found."})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SessionBackfill(
            timing_only_source(), client, api_base="https://api.openf1.org/v1"
        ).run(SESSION_KEY)
    assert set(result.errors) == {"sessions", "drivers", "laps", "stints", "pit", "race_control"}
    assert result.total == 0


# -- the service loop ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_service_backfills_once_and_then_leaves_it_alone() -> None:
    source = timing_only_source()
    seen: list[httpx.Request] = []
    async with fake_api(seen) as client:
        service = BackfillService(
            source, api_base="https://api.openf1.org/v1", client=client, poll_interval=0.01
        )
        service.start()
        for _ in range(50):
            if source.snapshot().session is not None:
                break
            await _yield()
        first = len(seen)
        for _ in range(20):
            await _yield()
        service.cancel()

    assert source.snapshot().session is not None
    assert service.runs == 1, "identity is present now; it must not keep fetching"
    assert len(seen) == first


@pytest.mark.asyncio
async def test_the_service_does_nothing_while_no_session_is_active() -> None:
    seen: list[httpx.Request] = []
    async with fake_api(seen) as client:
        service = BackfillService(
            OpenF1LiveSource(), api_base="https://api.openf1.org/v1", client=client, poll_interval=0.01
        )
        service.start()
        for _ in range(20):
            await _yield()
        service.cancel()
    assert seen == []


@pytest.mark.asyncio
async def test_a_session_switch_nudges_a_fresh_backfill() -> None:
    source = timing_only_source()
    seen: list[httpx.Request] = []
    async with fake_api(seen) as client:
        service = BackfillService(
            source, api_base="https://api.openf1.org/v1", client=client, poll_interval=5.0
        )
        source.set_session_switch_hook(service.nudge)
        service.start()
        for _ in range(50):
            if service.runs:
                break
            await _yield()
        assert service.runs == 1

        # A later session starts: its bucket has no identity of its own.
        source.ingest(TOPIC_LAPS, {"session_key": SESSION_KEY + 1, "driver_number": 16,
                                   "lap_number": 1, "_id": 99})
        for _ in range(50):
            if service.runs > 1:
                break
            await _yield()
        service.cancel()

    assert service.runs == 2, "the switch must trigger a pass without waiting for the poll"
    assert service.last_result is not None and service.last_result.session_key == SESSION_KEY + 1


async def _yield() -> None:
    import asyncio

    await asyncio.sleep(0.005)
