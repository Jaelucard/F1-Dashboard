"""Malformed upstream payloads: quarantine the record, never the snapshot.

The broker is an external system. A record with the wrong shape must be
counted and ignored; it must not merge into a shared ``None:None`` key, must
not blank a valid record, and must never make ``snapshot()`` raise.
"""

from __future__ import annotations

import pytest

from app.adapters.openf1_live import (
    TELEMETRY_HISTORY_PER_DRIVER,
    OpenF1LiveSource,
    natural_key,
    validate_record,
)
from scripts.sample_data import build_messages


# -- identity -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("topic", "payload"),
    [
        ("v1/laps", {"lap_number": 9, "lap_duration": 80.0}),          # no driver
        ("v1/laps", {"driver_number": 4, "lap_duration": 80.0}),       # no lap number
        ("v1/laps", {"driver_number": None, "lap_number": None}),      # nulls
        ("v1/stints", {"driver_number": 4}),                           # no stint number
        ("v1/position", {"position": 1}),                              # no driver
        ("v1/intervals", {"gap_to_leader": 1.0}),
        ("v1/car_data", {"speed": 300}),
        ("v1/location", {"x": 1, "y": 2}),
        ("v1/drivers", {"name_acronym": "VER"}),
        ("v1/sessions", {"session_name": "Practice 1"}),
        ("v1/race_control", {"flag": "RED"}),                          # no date, no _key
        ("v1/laps", {"driver_number": True, "lap_number": 9}),         # bool is not an int
        ("v1/laps", {"driver_number": 4.5, "lap_number": 9}),
        ("v1/laps", {"driver_number": "abc", "lap_number": 9}),
        ("v1/laps", {"driver_number": [4], "lap_number": 9}),
    ],
)
def test_records_missing_identity_are_quarantined(topic: str, payload: dict) -> None:
    source = OpenF1LiveSource()
    assert source.ingest(topic, payload) is False
    assert source.get_laps() == [] and source.get_positions() == []
    stats = source.stats()
    assert stats["quarantined_records"] == 1
    assert stats["topics"].get(topic, {}).get("records", 0) == 0


def test_two_unrelated_malformed_records_do_not_merge_into_one_key() -> None:
    """The None:None bug: two driverless laps used to become one record."""
    source = OpenF1LiveSource()
    source.ingest("v1/laps", {"lap_duration": 80.0})
    source.ingest("v1/laps", {"duration_sector_1": 28.0})
    assert source.get_laps() == []
    assert natural_key("v1/laps", {"lap_duration": 80.0}) is None
    assert source.stats()["quarantined_records"] == 2


def test_rest_style_records_without_key_or_id_merge_by_natural_key() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/laps", {"driver_number": 4, "lap_number": 9, "duration_sector_1": 28.1})
    source.ingest("v1/laps", {"driver_number": 4, "lap_number": 9, "lap_duration": 79.5})
    laps = source.get_laps()
    assert len(laps) == 1
    assert laps[0]["duration_sector_1"] == 28.1 and laps[0]["lap_duration"] == 79.5
    assert source.stats()["quarantined_records"] == 0


def test_numeric_strings_in_identity_fields_are_normalised() -> None:
    source = OpenF1LiveSource()
    assert source.ingest("v1/laps", {"driver_number": "4", "lap_number": "9", "lap_duration": 79.5})
    assert source.get_laps()[0]["driver_number"] == 4
    assert source.snapshot().drivers[0].driver_number == 4


# -- field-level normalisation ------------------------------------------------


def test_malformed_fields_are_stripped_and_counted_not_fatal() -> None:
    source = OpenF1LiveSource()
    assert source.ingest("v1/laps", {"driver_number": 4, "lap_number": 9,
                                     "lap_duration": "fast", "duration_sector_1": 28.1})
    lap = source.get_laps()[0]
    assert "lap_duration" not in lap
    assert lap["duration_sector_1"] == 28.1
    assert source.stats()["malformed_fields"] == 1

    driver = source.snapshot().drivers[0]
    assert driver.last_lap_duration is None, "the lap has no usable duration"
    # The sector time survives and is shown: with lap_duration stripped this is
    # a lap in progress, and sectors come from the lap in progress so the strip
    # fills in live. Only the unusable field was lost.
    assert driver.sector_1 == 28.1


def test_a_malformed_revision_does_not_blank_a_valid_value() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/laps", {"_key": "k", "_id": 1, "driver_number": 4, "lap_number": 9, "lap_duration": 79.5})
    source.ingest("v1/laps", {"_key": "k", "_id": 2, "driver_number": 4, "lap_number": 9, "lap_duration": {"oops": 1}})
    assert source.get_laps()[0]["lap_duration"] == 79.5


@pytest.mark.parametrize(
    ("topic", "payload", "field"),
    [
        ("v1/position", {"driver_number": 4, "position": "P1"}, "position"),
        ("v1/intervals", {"driver_number": 4, "gap_to_leader": [1]}, "gap_to_leader"),
        ("v1/intervals", {"driver_number": 4, "interval": {"x": 1}}, "interval"),
        ("v1/car_data", {"driver_number": 4, "speed": "fast", "drs": "on"}, "speed"),
        ("v1/stints", {"driver_number": 4, "stint_number": 1, "lap_start": "five"}, "lap_start"),
        ("v1/pit", {"driver_number": 4, "lap_number": "x", "date": "2026-09-04T11:00:00+00:00"}, "lap_number"),
        ("v1/drivers", {"driver_number": 4, "team_colour": 4781}, "team_colour"),
        ("v1/sessions", {"session_key": 1, "year": "twenty"}, "year"),
        ("v1/race_control", {"date": "2026-09-04T11:00:00+00:00", "lap_number": 1.5}, "lap_number"),
    ],
)
def test_wrong_typed_fields_are_dropped_per_topic(topic: str, payload: dict, field: str) -> None:
    record, dropped = validate_record(topic, payload)
    assert record is not None
    assert field not in record
    assert field in dropped


def test_gap_strings_are_valid_values() -> None:
    """'+1 LAP' really arrives as a string, so strings must survive validation."""
    record, dropped = validate_record("v1/intervals", {"driver_number": 4, "gap_to_leader": "+1 LAP"})
    assert record == {"driver_number": 4, "gap_to_leader": "+1 LAP"} and dropped == []


def test_snapshot_survives_a_grid_of_garbage() -> None:
    source = OpenF1LiveSource()
    for topic, payload in build_messages():
        source.ingest(topic, payload)
    garbage = [
        ("v1/position", {"driver_number": 100, "position": "P1", "session_key": 9999}),
        ("v1/laps", {"driver_number": 100, "lap_number": 12, "lap_duration": "abc", "session_key": 9999}),
        ("v1/stints", {"driver_number": 100, "stint_number": 1, "compound": 7, "lap_start": None, "session_key": 9999}),
        ("v1/pit", {"driver_number": 100, "lap_number": {"a": 1}, "date": 5, "session_key": 9999}),
        ("v1/car_data", {"driver_number": 100, "speed": None, "drs": "8", "session_key": 9999}),
        ("v1/race_control", {"date": 12345, "flag": ["YELLOW"], "session_key": 9999}),
        ("v1/drivers", {"driver_number": 100, "team_colour": None, "name_acronym": 1, "session_key": 9999}),
        ("v1/sessions", {"session_key": 9999, "date_start": 42, "gmt_offset": 2}),
        ("v1/laps", "not a dict"),
        ("v1/laps", None),
        ("v1/laps", {"_key": None, "_id": "x", "driver_number": 100, "lap_number": 13, "session_key": 9999}),
    ]
    for topic, payload in garbage:
        source.ingest(topic, payload)

    snapshot = source.snapshot()
    assert len(snapshot.drivers) == 22
    leader = snapshot.drivers[0]
    assert leader.driver_number == 100
    assert leader.position == 1, "a malformed position update must not blank the valid one"
    assert leader.last_lap_duration == 79.681
    assert leader.name_acronym == "VER"
    assert snapshot.track_flag == "YELLOW"
    assert snapshot.adapter is not None
    assert snapshot.adapter.quarantined_records >= 3
    assert snapshot.adapter.malformed_fields >= 5


def test_non_dict_payloads_are_counted_as_malformed() -> None:
    source = OpenF1LiveSource()
    for payload in ([1, 2, 3], "text", None, 42):
        assert source.ingest("v1/laps", payload) is False
    assert source.stats()["quarantined_records"] == 4


# -- bounded telemetry -------------------------------------------------------


def test_location_history_is_bounded_per_driver_even_with_unique_keys() -> None:
    """Every MQTT location message has its own _key; storing each one is a leak."""
    source = OpenF1LiveSource()
    for i in range(TELEMETRY_HISTORY_PER_DRIVER * 5):
        source.ingest("v1/location", {"_key": f"loc-{i}", "_id": i, "driver_number": 4,
                                      "x": i, "y": 0, "date": "2026-09-04T11:00:00+00:00"})
        source.ingest("v1/car_data", {"_key": f"car-{i}", "_id": i, "driver_number": 4,
                                      "speed": i, "drs": 0})

    locations = source.get_locations()
    assert len(locations) == TELEMETRY_HISTORY_PER_DRIVER
    assert locations[-1]["x"] == TELEMETRY_HISTORY_PER_DRIVER * 5 - 1, "newest sample kept"
    assert len(source.get_car_data()) == TELEMETRY_HISTORY_PER_DRIVER
    assert source.snapshot().drivers[0].speed == TELEMETRY_HISTORY_PER_DRIVER * 5 - 1

    topics = source.stats()["topics"]
    assert topics["v1/location"]["records"] == TELEMETRY_HISTORY_PER_DRIVER
    assert topics["v1/car_data"]["records"] == TELEMETRY_HISTORY_PER_DRIVER


def test_position_and_intervals_keep_only_the_latest_per_driver() -> None:
    source = OpenF1LiveSource()
    for i in range(500):
        source.ingest("v1/position", {"_key": f"pos-{i}", "_id": i, "driver_number": 4, "position": 1 + i % 3})
        source.ingest("v1/intervals", {"_key": f"int-{i}", "_id": i, "driver_number": 4, "gap_to_leader": i})
    assert source.stats()["topics"]["v1/position"]["records"] == 1
    assert source.stats()["topics"]["v1/intervals"]["records"] == 1
    assert source.snapshot().drivers[0].gap_to_leader == 499


def test_an_older_telemetry_sample_cannot_replace_a_newer_one() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/car_data", {"_key": "b", "_id": 20, "driver_number": 4, "speed": 300})
    source.ingest("v1/car_data", {"_key": "a", "_id": 10, "driver_number": 4, "speed": 100})
    assert source.snapshot().drivers[0].speed == 300
    assert source.stats()["topics"]["v1/car_data"]["stale_drops"] == 1


def test_keyed_stores_have_a_hard_cap() -> None:
    """A defensive bound: if a topic's _key turns out to be per-message, memory still cannot grow forever."""
    source = OpenF1LiveSource()
    for i in range(7000):
        source.ingest("v1/laps", {"_key": f"lap-{i}", "_id": i, "driver_number": 4, "lap_number": i})
    assert source.stats()["topics"]["v1/laps"]["records"] <= 5000
    assert source.stats()["topics"]["v1/laps"]["evicted"] >= 2000


def test_race_control_is_bounded() -> None:
    source = OpenF1LiveSource()
    for i in range(1000):
        source.ingest("v1/race_control", {"_key": f"rc-{i}", "_id": i,
                                          "date": f"2026-09-04T11:{i // 60 % 60:02d}:{i % 60:02d}+00:00",
                                          "category": "Other", "message": f"m{i}"})
    assert source.stats()["topics"]["v1/race_control"]["records"] <= 500
    assert len(source.snapshot().race_control) <= 60


def test_unknown_topics_are_bounded() -> None:
    source = OpenF1LiveSource()
    for i in range(1000):
        source.ingest("v1/something_new", {"_key": f"n-{i}", "value": i})
    assert source.stats()["topics"]["v1/something_new"]["records"] <= 200


# -- timestamps and ordering -----------------------------------------------


def test_race_control_orders_by_parsed_utc_time_not_by_string() -> None:
    source = OpenF1LiveSource()
    # String order puts "12:00+02:00" after "11:00+00:00"; in UTC it is earlier.
    source.ingest("v1/race_control", {"date": "2026-09-04T11:00:00+00:00", "category": "Flag",
                                      "flag": "CLEAR", "scope": "Track"})
    source.ingest("v1/race_control", {"date": "2026-09-04T12:00:00+02:00", "category": "Flag",
                                      "flag": "YELLOW", "scope": "Track"})
    snapshot = source.snapshot()
    assert snapshot.track_flag is None, "the CLEAR at 11:00Z is the later message"
    assert [m.flag for m in snapshot.race_control] == ["YELLOW", "CLEAR"]


def test_race_control_uses_id_as_a_tie_breaker() -> None:
    source = OpenF1LiveSource()
    same = "2026-09-04T11:00:00+00:00"
    source.ingest("v1/race_control", {"_key": "b", "_id": 2, "date": same, "category": "Flag",
                                      "flag": "CLEAR", "scope": "Track"})
    source.ingest("v1/race_control", {"_key": "a", "_id": 1, "date": same, "category": "Flag",
                                      "flag": "YELLOW", "scope": "Track"})
    assert source.snapshot().track_flag is None
    assert [m.flag for m in source.snapshot().race_control] == ["YELLOW", "CLEAR"]


def test_race_control_with_a_malformed_date_never_becomes_the_latest() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/race_control", {"date": "2026-09-04T11:00:00+00:00", "category": "Flag",
                                      "flag": "YELLOW", "scope": "Track"})
    source.ingest("v1/race_control", {"_key": "bad", "date": "yesterday-ish", "category": "Flag",
                                      "flag": "CLEAR", "scope": "Track"})
    source.ingest("v1/race_control", {"_key": "none", "category": "Flag", "flag": "RED",
                                      "scope": "Track"})
    snapshot = source.snapshot()
    assert snapshot.track_flag == "YELLOW"
    assert len(snapshot.race_control) == 3, "kept, just not trusted for ordering"
    assert snapshot.race_control[-1].flag == "YELLOW"


def test_a_duplicate_delivery_of_the_same_message_is_idempotent() -> None:
    source = OpenF1LiveSource()
    payload = {"_key": "k", "_id": 5, "driver_number": 4, "lap_number": 9, "lap_duration": 79.5}
    source.ingest("v1/laps", payload)
    source.ingest("v1/laps", dict(payload))
    assert len(source.get_laps()) == 1
    assert source.stats()["topics"]["v1/laps"]["duplicates"] == 1


def test_partial_revision_without_id_still_merges() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/laps", {"_key": "k", "_id": 5, "driver_number": 4, "lap_number": 9, "lap_duration": 79.5})
    source.ingest("v1/laps", {"_key": "k", "driver_number": 4, "lap_number": 9, "duration_sector_3": 25.0})
    lap = source.get_laps()[0]
    assert lap["lap_duration"] == 79.5 and lap["duration_sector_3"] == 25.0


def test_snapshot_records_per_driver_build_failures_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt and braces: if one driver's state cannot be built, the rest still render."""
    from app.adapters import openf1_live

    source = OpenF1LiveSource()
    source.ingest("v1/position", {"driver_number": 4, "position": 1})
    source.ingest("v1/position", {"driver_number": 5, "position": 2})

    real = openf1_live.DriverState

    def exploding(**kwargs: object) -> object:
        if kwargs.get("driver_number") == 4:
            raise ValueError("boom")
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(openf1_live, "DriverState", exploding)
    snapshot = source.snapshot()
    assert [d.driver_number for d in snapshot.drivers] == [5]
    assert snapshot.adapter is not None and snapshot.adapter.driver_build_errors == 1
