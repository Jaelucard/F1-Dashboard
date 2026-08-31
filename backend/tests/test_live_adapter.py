"""openf1_live.py: the merge rules, and the Phase 2 acceptance criteria."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.adapters.openf1_live import (
    OpenF1LiveSource,
    TopicStore,
    compute_tyre_age,
    natural_key,
    record_key,
)
from scripts.sample_data import build_messages, build_source


# -- _key upsert: the rule the docs call out ---------------------------------


def test_same_key_updates_one_record_instead_of_appending() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/laps", {"_key": "k", "_id": 1, "driver_number": 4,
                              "lap_number": 9, "duration_sector_1": 28.1})
    source.ingest("v1/laps", {"_key": "k", "_id": 2, "driver_number": 4,
                              "lap_number": 9, "duration_sector_2": 26.0,
                              "lap_duration": 79.5})

    laps = source.get_laps()
    assert len(laps) == 1, "two messages with one _key must be one record"
    # The revision must not blank fields it did not mention.
    assert laps[0]["duration_sector_1"] == 28.1
    assert laps[0]["duration_sector_2"] == 26.0
    assert laps[0]["lap_duration"] == 79.5


def test_different_keys_are_separate_records() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/laps", {"_key": "a", "driver_number": 4, "lap_number": 9})
    source.ingest("v1/laps", {"_key": "b", "driver_number": 4, "lap_number": 10})
    assert len(source.get_laps()) == 2


def test_a_lower_id_cannot_overwrite_fresher_data() -> None:
    """_id is ever-increasing, so an out-of-order message must be dropped."""
    source = OpenF1LiveSource()
    source.ingest("v1/laps", {"_key": "k", "_id": 20, "driver_number": 4,
                              "lap_number": 9, "lap_duration": 79.5})
    source.ingest("v1/laps", {"_key": "k", "_id": 10, "driver_number": 4,
                              "lap_number": 9, "lap_duration": 999.9})

    assert source.get_laps()[0]["lap_duration"] == 79.5
    assert source.stats()["topics"]["v1/laps"]["stale_drops"] == 1


def test_equal_ids_still_apply() -> None:
    """Only a strictly lower _id is stale; a redelivery should still merge."""
    source = OpenF1LiveSource()
    source.ingest("v1/laps", {"_key": "k", "_id": 5, "driver_number": 4, "lap_number": 9})
    source.ingest("v1/laps", {"_key": "k", "_id": 5, "driver_number": 4,
                              "lap_number": 9, "lap_duration": 79.5})
    assert source.get_laps()[0]["lap_duration"] == 79.5


def test_messages_without_a_key_fall_back_to_a_natural_key() -> None:
    """REST payloads carry no _key, and replay/historical reuse this merge."""
    source = OpenF1LiveSource()
    source.ingest("v1/laps", {"driver_number": 4, "lap_number": 9, "duration_sector_1": 28.1})
    source.ingest("v1/laps", {"driver_number": 4, "lap_number": 9, "lap_duration": 79.5})
    source.ingest("v1/laps", {"driver_number": 4, "lap_number": 10})

    laps = source.get_laps()
    assert len(laps) == 2, "same driver+lap merges; a new lap does not"
    assert laps[0]["duration_sector_1"] == 28.1 and laps[0]["lap_duration"] == 79.5


@pytest.mark.parametrize(
    ("topic", "payload", "expected"),
    [
        ("v1/laps", {"driver_number": 4, "lap_number": 9}, "4:9"),
        ("v1/stints", {"driver_number": 4, "stint_number": 2}, "4:2"),
        ("v1/position", {"driver_number": 4}, "4"),
        ("v1/intervals", {"driver_number": 4}, "4"),
        ("v1/sessions", {"session_key": 9999}, "9999"),
    ],
)
def test_natural_keys(topic: str, payload: dict, expected: str) -> None:
    assert natural_key(topic, payload) == expected


def test_explicit_key_wins_over_the_natural_one() -> None:
    assert record_key("v1/laps", {"_key": "abc", "driver_number": 4, "lap_number": 9}) == "abc"
    assert record_key("v1/laps", {"_key": "  ", "driver_number": 4, "lap_number": 9}) == "4:9"


def test_store_counts_inserts_and_updates() -> None:
    store = TopicStore("v1/laps")
    assert store.upsert({"_key": "a", "driver_number": 1}) is True
    assert store.upsert({"_key": "a", "driver_number": 1}) is True
    assert store.inserts == 1 and store.updates == 1


# -- tyre age ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("current_lap", "lap_start", "age_at_start", "expected"),
    [
        (20, 15, 0, 5),      # five laps into a new set
        (15, 15, 0, 0),      # first lap of a new set
        (20, 15, 3, 8),      # scrubbed set: three laps already on it
        (15, 15, 3, 3),      # first lap of a scrubbed set
        (None, 15, 4, 4),    # no lap count yet: fall back to age at fitting
        (10, 15, 0, 0),      # stint announced early: never negative
        (20, None, 0, None), # no stint known at all
        (20, 15, None, 5),   # null age_at_start behaves as zero
    ],
)
def test_compute_tyre_age(
    current_lap: int | None, lap_start: int | None, age_at_start: int | None, expected: int | None
) -> None:
    assert compute_tyre_age(current_lap, lap_start, age_at_start) == expected


# -- snapshot construction --------------------------------------------------


def test_last_lap_is_the_last_completed_one_not_the_lap_in_progress() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/laps", {"driver_number": 4, "lap_number": 9, "lap_duration": 79.5})
    # Lap 10 is under way: it has a sector but no duration yet.
    source.ingest("v1/laps", {"driver_number": 4, "lap_number": 10, "duration_sector_1": 27.9})

    driver = source.snapshot().drivers[0]
    assert driver.last_lap_duration == 79.5
    assert driver.lap_number == 10, "the current lap number still advances"


def test_best_lap_is_the_minimum_completed_lap() -> None:
    source = OpenF1LiveSource()
    for lap, duration in [(1, 82.0), (2, 79.5), (3, 80.1)]:
        source.ingest("v1/laps", {"driver_number": 4, "lap_number": lap, "lap_duration": duration})
    driver = source.snapshot().drivers[0]
    assert driver.best_lap_duration == 79.5
    assert driver.last_lap_duration == 80.1


def test_session_best_is_the_fastest_lap_of_anyone() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/laps", {"driver_number": 1, "lap_number": 1, "lap_duration": 80.5})
    source.ingest("v1/laps", {"driver_number": 2, "lap_number": 1, "lap_duration": 79.2})
    assert source.snapshot().session_best_lap == 79.2


def test_drivers_are_sorted_by_position_with_unclassified_last() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/position", {"driver_number": 44, "position": 3})
    source.ingest("v1/position", {"driver_number": 1, "position": 1})
    source.ingest("v1/laps", {"driver_number": 99, "lap_number": 1})  # no position
    source.ingest("v1/position", {"driver_number": 16, "position": 2})

    order = [d.driver_number for d in source.snapshot().drivers]
    assert order == [1, 16, 44, 99]


def test_interval_behind_is_the_next_cars_interval_ahead() -> None:
    source = OpenF1LiveSource()
    for number, position, interval in [(1, 1, None), (2, 2, 0.5), (3, 3, 1.2)]:
        source.ingest("v1/position", {"driver_number": number, "position": position})
        source.ingest("v1/intervals", {"driver_number": number, "interval": interval,
                                       "gap_to_leader": 0})
    drivers = source.snapshot().drivers
    assert drivers[0].interval_behind == 0.5
    assert drivers[1].interval_behind == 1.2
    assert drivers[2].interval_behind is None, "the last car has nobody behind"


def test_a_driver_appears_even_before_the_entry_list_arrives() -> None:
    """Timing can start before v1/drivers, and a blank board would be wrong."""
    source = OpenF1LiveSource()
    source.ingest("v1/position", {"driver_number": 77, "position": 1})
    drivers = source.snapshot().drivers
    assert len(drivers) == 1
    assert drivers[0].driver_number == 77
    assert drivers[0].name_acronym is None


def test_lapped_car_gap_string_is_preserved_not_coerced() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/intervals", {"driver_number": 5, "gap_to_leader": "+1 LAP", "interval": None})
    assert source.snapshot().drivers[0].gap_to_leader == "+1 LAP"


def test_aero_raw_is_carried_through_without_interpretation() -> None:
    """2026 has no DRS. The legacy integer is stored, never decoded."""
    source = OpenF1LiveSource()
    source.ingest("v1/car_data", {"driver_number": 4, "drs": 12, "speed": 330})
    driver = source.snapshot().drivers[0]
    assert driver.aero_raw == 12
    assert driver.speed == 330
    assert not hasattr(driver, "drs_active")
    assert "drs" not in driver.model_dump()


# -- race control -----------------------------------------------------------


def test_track_flag_is_the_latest_and_green_clears_it() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/race_control", {"date": "2026-09-04T11:00:00+00:00", "category": "Flag",
                                      "flag": "YELLOW", "scope": "Track"})
    assert source.snapshot().track_flag == "YELLOW"

    source.ingest("v1/race_control", {"date": "2026-09-04T11:05:00+00:00", "category": "Flag",
                                      "flag": "CLEAR", "scope": "Track"})
    assert source.snapshot().track_flag is None


def test_driver_scoped_flags_do_not_become_the_track_flag() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/race_control", {"date": "2026-09-04T11:00:00+00:00", "category": "Flag",
                                      "flag": "BLUE", "scope": "Driver", "driver_number": 5})
    assert source.snapshot().track_flag is None


def test_partial_aero_badge_tracks_race_control() -> None:
    """2026: race control can enable front-Straight/rear-Corner in the wet."""
    source = OpenF1LiveSource()
    assert source.snapshot().partial_aero is False

    source.ingest("v1/race_control", {"date": "2026-09-04T11:10:00+00:00", "category": "Other",
                                      "message": "PARTIAL AERO MODE ENABLED"})
    assert source.snapshot().partial_aero is True

    source.ingest("v1/race_control", {"date": "2026-09-04T11:20:00+00:00", "category": "Other",
                                      "message": "PARTIAL AERO MODE DISABLED"})
    assert source.snapshot().partial_aero is False


def test_legacy_drs_race_control_category_does_not_light_the_aero_badge() -> None:
    """The Drs category still exists in the feed and means nothing in 2026."""
    source = OpenF1LiveSource()
    source.ingest("v1/race_control", {"date": "2026-09-04T11:10:00+00:00", "category": "Drs",
                                      "message": "DRS ENABLED"})
    assert source.snapshot().partial_aero is False


# -- misc -------------------------------------------------------------------


def test_non_dict_payloads_are_ignored_without_raising() -> None:
    source = OpenF1LiveSource()
    for payload in ([1, 2, 3], "text", None, 42):
        assert source.ingest("v1/laps", payload) is False
    assert source.get_laps() == []


def test_since_filters_by_the_records_own_timestamp() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/intervals", {"driver_number": 1, "date": "2026-09-04T11:00:00+00:00"})
    source.ingest("v1/intervals", {"driver_number": 2, "date": "2026-09-04T11:30:00+00:00"})

    cutoff = datetime(2026, 9, 4, 11, 15, tzinfo=timezone.utc)
    kept = source.get_intervals(since=cutoff)
    assert [record["driver_number"] for record in kept] == [2]


def test_unknown_topics_are_still_stored() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/something_new", {"driver_number": 1, "date": "x"})
    assert source.stats()["topics"]["v1/something_new"]["records"] == 1


# -- Phase 2 acceptance ------------------------------------------------------


def test_acceptance_injector_produces_a_full_grid() -> None:
    """The fake message injector feeds sample payloads; the state is complete."""
    snapshot = build_source().snapshot()

    assert len(snapshot.drivers) >= 20, "20-plus drivers required"

    # Every column the Tier A leaderboard renders has a value where it should.
    leader = snapshot.drivers[0]
    assert leader.position == 1
    assert leader.name_acronym == "VER"
    assert leader.team_name == "Red Bull Racing"
    assert leader.team_colour == "4781D7"
    assert leader.last_lap_duration is not None
    assert leader.best_lap_duration == snapshot.session_best_lap
    assert leader.compound is not None and leader.tyre_age is not None

    # The updated lap value: lap 12 arrived twice under one _key and the
    # revision must be what the leaderboard shows.
    assert leader.last_lap_duration == 79.681
    assert leader.sector_1 == 27.8, "kept from the first message"
    assert leader.sector_3 == 25.4, "added by the revision"

    # Team colours: present from the API, and absent so teams.ts can fall back.
    assert any(d.team_colour is None for d in snapshot.drivers)
    assert {d.team_name for d in snapshot.drivers} >= {"Audi", "Cadillac"}

    # The awkward real-world cases.
    assert snapshot.drivers[-1].gap_to_leader == "+1 LAP"
    assert any(d.in_pit for d in snapshot.drivers)
    assert any(d.is_pit_out_lap for d in snapshot.drivers)
    assert any(
        d.last_lap_duration and d.best_lap_duration and d.last_lap_duration > d.best_lap_duration
        for d in snapshot.drivers
    )
    assert snapshot.track_flag == "YELLOW"
    assert snapshot.session is not None and snapshot.session.circuit_short_name == "Monza"


def test_acceptance_replaying_the_messages_twice_is_idempotent() -> None:
    """Redelivery must not duplicate drivers or corrupt lap times."""
    source = OpenF1LiveSource()
    for _ in range(2):
        for topic, payload in build_messages():
            source.ingest(topic, payload)

    snapshot = source.snapshot()
    assert len(snapshot.drivers) == 22
    numbers = [d.driver_number for d in snapshot.drivers]
    assert len(numbers) == len(set(numbers))
    assert snapshot.drivers[0].last_lap_duration == 79.681
