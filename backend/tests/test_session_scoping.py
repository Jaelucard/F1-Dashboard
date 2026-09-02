"""Session scoping: a new session must start from a clean board.

OpenF1 tags every MQTT payload with ``session_key``. A message from FP1 that
lands after FP2 has started (broker replay, a late retransmit, a paho reconnect
that delivers a backlog) must never appear on FP2's leaderboard, and FP2 must
not inherit FP1's laps, pits, flags or telemetry.
"""

from __future__ import annotations

from app.adapters.openf1_live import MAX_RETAINED_SESSIONS, OpenF1LiveSource


def feed_session(source: OpenF1LiveSource, key: int, driver: int) -> None:
    """A minimal but complete session's worth of data for one driver."""
    source.ingest("v1/sessions", {"session_key": key, "session_name": f"S{key}",
                                  "date_start": f"2026-09-0{key}T11:30:00+00:00"})
    source.ingest("v1/position", {"session_key": key, "driver_number": driver, "position": 1,
                                  "date": f"2026-09-0{key}T11:45:00+00:00"})
    source.ingest("v1/intervals", {"session_key": key, "driver_number": driver,
                                   "gap_to_leader": 0, "interval": None})
    source.ingest("v1/laps", {"session_key": key, "driver_number": driver, "lap_number": 3,
                              "lap_duration": 80.0 + key})
    source.ingest("v1/stints", {"session_key": key, "driver_number": driver, "stint_number": 1,
                                "lap_start": 1, "compound": "SOFT", "tyre_age_at_start": 0})
    source.ingest("v1/pit", {"session_key": key, "driver_number": driver, "lap_number": 2,
                             "date": f"2026-09-0{key}T11:40:00+00:00"})
    source.ingest("v1/race_control", {"session_key": key, "date": f"2026-09-0{key}T11:41:00+00:00",
                                      "category": "Flag", "flag": "YELLOW", "scope": "Track",
                                      "message": f"YELLOW {key}"})
    source.ingest("v1/location", {"session_key": key, "driver_number": driver, "x": key, "y": 0,
                                  "date": f"2026-09-0{key}T11:45:01+00:00"})
    source.ingest("v1/car_data", {"session_key": key, "driver_number": driver, "speed": 300 + key,
                                  "drs": 0, "date": f"2026-09-0{key}T11:45:01+00:00"})


def test_a_new_session_starts_with_a_clean_board() -> None:
    source = OpenF1LiveSource()
    feed_session(source, key=1, driver=4)
    assert [d.driver_number for d in source.snapshot().drivers] == [4]

    feed_session(source, key=2, driver=5)
    snapshot = source.snapshot()

    assert snapshot.session is not None and snapshot.session.session_key == 2
    assert [d.driver_number for d in snapshot.drivers] == [5], "FP1's driver must not linger"
    driver = snapshot.drivers[0]
    assert driver.last_lap_duration == 82.0
    assert driver.pit_count == 1, "only FP2's own pit stop"
    assert driver.speed == 302
    assert [m.message for m in snapshot.race_control] == ["YELLOW 2"]
    assert snapshot.track_flag == "YELLOW"
    assert source.get_locations() and all(r["x"] == 2 for r in source.get_locations())
    assert all(lap["session_key"] == 2 for lap in source.get_laps())


def test_a_late_message_from_an_older_session_cannot_touch_the_new_one() -> None:
    source = OpenF1LiveSource()
    feed_session(source, key=1, driver=4)
    feed_session(source, key=2, driver=5)

    # FP1 stragglers arrive after FP2 has started.
    source.ingest("v1/laps", {"session_key": 1, "driver_number": 4, "lap_number": 4, "lap_duration": 70.0})
    source.ingest("v1/position", {"session_key": 1, "driver_number": 4, "position": 1})
    source.ingest("v1/race_control", {"session_key": 1, "date": "2026-09-01T11:50:00+00:00",
                                      "category": "Flag", "flag": "RED", "scope": "Track"})

    snapshot = source.snapshot()
    assert [d.driver_number for d in snapshot.drivers] == [5]
    assert snapshot.session_best_lap == 82.0, "FP1's faster lap must not become FP2's session best"
    assert snapshot.track_flag == "YELLOW"
    assert source.stats()["late_session_messages"] == 3
    assert snapshot.adapter is not None
    assert snapshot.adapter.active_session_key == 2
    assert snapshot.adapter.late_session_messages == 3


def test_the_active_session_is_the_highest_key_not_the_last_seen() -> None:
    """Delivery order is not identity: an FP1 backlog after FP2 data must not win."""
    source = OpenF1LiveSource()
    feed_session(source, key=2, driver=5)
    feed_session(source, key=1, driver=4)

    snapshot = source.snapshot()
    assert snapshot.session is not None and snapshot.session.session_key == 2
    assert [d.driver_number for d in snapshot.drivers] == [5]
    assert source.stats()["active_session_key"] == 2


def test_a_sessions_announcement_alone_does_not_switch_sessions() -> None:
    """OpenF1 can publish the weekend's schedule early; only timing data counts."""
    source = OpenF1LiveSource()
    feed_session(source, key=1, driver=4)
    source.ingest("v1/sessions", {"session_key": 2, "session_name": "Practice 2"})
    source.ingest("v1/drivers", {"session_key": 2, "driver_number": 5, "name_acronym": "FIV"})

    snapshot = source.snapshot()
    assert snapshot.session is not None and snapshot.session.session_key == 1
    assert [d.driver_number for d in snapshot.drivers] == [4]

    # The moment FP2 timing starts, FP2 is the session - with its entry list.
    source.ingest("v1/position", {"session_key": 2, "driver_number": 5, "position": 1})
    snapshot = source.snapshot()
    assert snapshot.session is not None and snapshot.session.session_key == 2
    assert snapshot.session.session_name == "Practice 2"
    assert [d.name_acronym for d in snapshot.drivers] == ["FIV"]


def test_records_without_a_session_key_attach_to_the_active_session() -> None:
    source = OpenF1LiveSource()
    feed_session(source, key=1, driver=4)
    source.ingest("v1/position", {"driver_number": 9, "position": 2})
    assert [d.driver_number for d in source.snapshot().drivers] == [4, 9]


def test_only_a_bounded_number_of_sessions_is_retained() -> None:
    source = OpenF1LiveSource()
    for key in range(1, 6):
        feed_session(source, key=key, driver=key)
    stats = source.stats()
    assert len(stats["sessions"]) <= MAX_RETAINED_SESSIONS
    assert stats["active_session_key"] == 5
    assert 1 not in stats["sessions"]


def test_a_message_for_an_evicted_session_is_dropped_and_counted() -> None:
    source = OpenF1LiveSource()
    for key in range(1, 5):
        feed_session(source, key=key, driver=key)
    assert source.ingest("v1/laps", {"session_key": 1, "driver_number": 1, "lap_number": 9}) is False
    assert source.stats()["late_session_messages"] == 1
    assert [d.driver_number for d in source.snapshot().drivers] == [4]


def test_session_switch_is_reported_in_stats() -> None:
    source = OpenF1LiveSource()
    feed_session(source, key=1, driver=4)
    feed_session(source, key=2, driver=5)
    stats = source.stats()
    assert stats["session_switches"] == 1
    assert set(stats["sessions"]) == {1, 2}
