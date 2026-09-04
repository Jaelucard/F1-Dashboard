"""openf1_live.py: the merge rules, and the Phase 2 acceptance criteria."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.adapters.openf1_live import (
    TOPIC_LAPS,
    OpenF1LiveSource,
    TopicStore,
    compute_tyre_age,
    natural_key,
    normalise_segments,
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


# -- track map: latest location per driver ----------------------------------


def test_driver_state_carries_latest_location() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/drivers", {"driver_number": 1, "name_acronym": "ONE", "session_key": 5})
    source.ingest("v1/position", {"driver_number": 1, "position": 1, "date": "2026-09-04T11:00:00+00:00", "session_key": 5})
    source.ingest("v1/location", {"driver_number": 1, "x": 1.0, "y": 2.0, "z": 3.0,
                                  "date": "2026-09-04T11:00:01+00:00", "session_key": 5})
    source.ingest("v1/location", {"driver_number": 1, "x": 10.0, "y": 20.0, "z": 3.0,
                                  "date": "2026-09-04T11:00:02+00:00", "session_key": 5})

    driver = source.snapshot().drivers[0]
    assert (driver.x, driver.y, driver.location_at) == (10.0, 20.0, "2026-09-04T11:00:02+00:00")


def test_driver_without_location_has_no_coordinates() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/drivers", {"driver_number": 1, "name_acronym": "ONE", "session_key": 5})
    source.ingest("v1/position", {"driver_number": 1, "position": 1, "date": "2026-09-04T11:00:00+00:00", "session_key": 5})

    driver = source.snapshot().drivers[0]
    assert driver.x is None and driver.y is None and driver.location_at is None


def test_sample_data_places_every_car_but_the_pitted_one_on_the_loop() -> None:
    state = build_source().snapshot()
    with_location = [d for d in state.drivers if d.x is not None and d.y is not None]
    without = [d for d in state.drivers if d.x is None]
    assert len(with_location) == len(state.drivers) - 1
    assert len(without) == 1 and without[0].in_pit is True
    # No bundled outline for the sample session: the map must use the fallback trace.
    assert state.session is not None and state.session.circuit_key is None


# -- mini-sectors ----------------------------------------------------------------


def _lap(number: int, **fields: object) -> dict[str, object]:
    return {"session_key": 1, "driver_number": 16, "lap_number": number, **fields}


def test_sectors_come_from_the_lap_in_progress_while_last_lap_stays_completed() -> None:
    """The strip must fill in live. Reading sectors from the last *completed*
    lap would leave it showing the previous lap until this one finished."""
    source = OpenF1LiveSource()
    source.ingest(TOPIC_LAPS, _lap(11, _id=1, lap_duration=80.5,
                                   duration_sector_1=28.0, duration_sector_2=26.0,
                                   duration_sector_3=26.5,
                                   segments_sector_1=[2049] * 8))
    # Lap 12 is under way: mini-sectors only, no times at all yet.
    source.ingest(TOPIC_LAPS, _lap(12, _id=2, segments_sector_1=[2049, 2051, 2048]))

    driver = source.snapshot().drivers[0]
    assert driver.segments_sector_1 == [2049, 2051, 2048], "the lap in progress"
    assert driver.sector_1 is None, "no sector 1 time on the lap in progress yet"
    assert driver.last_lap_duration == 80.5, "still the completed lap"
    assert driver.best_lap_duration == 80.5


def test_a_lap_in_progress_with_a_sector_time_but_no_segments_is_still_chosen() -> None:
    source = OpenF1LiveSource()
    source.ingest(TOPIC_LAPS, _lap(11, _id=1, lap_duration=80.5, duration_sector_1=28.0))
    source.ingest(TOPIC_LAPS, _lap(12, _id=2, duration_sector_1=27.4))

    driver = source.snapshot().drivers[0]
    assert driver.sector_1 == 27.4
    assert driver.last_lap_duration == 80.5


def test_a_latest_lap_with_nothing_yet_falls_back_to_the_completed_lap() -> None:
    """A bare lap record (a new lap has started, nothing timed) must not blank
    the strip - the previous lap's sectors stay up until there is a reading."""
    source = OpenF1LiveSource()
    source.ingest(TOPIC_LAPS, _lap(11, _id=1, lap_duration=80.5, duration_sector_2=26.0,
                                   segments_sector_2=[2051] * 7))
    source.ingest(TOPIC_LAPS, _lap(12, _id=2))

    driver = source.snapshot().drivers[0]
    assert driver.sector_2 == 26.0
    assert driver.segments_sector_2 == [2051] * 7


def test_an_empty_segments_array_does_not_count_as_sector_data() -> None:
    source = OpenF1LiveSource()
    source.ingest(TOPIC_LAPS, _lap(11, _id=1, lap_duration=80.5, duration_sector_3=26.5))
    source.ingest(TOPIC_LAPS, _lap(12, _id=2, segments_sector_1=[], segments_sector_2=[]))

    assert source.snapshot().drivers[0].sector_3 == 26.5


def test_non_integer_segment_codes_are_coerced_to_zero() -> None:
    """0 is OpenF1's own "not available", so an unreadable code degrades to
    "nothing known" rather than to a colour."""
    source = OpenF1LiveSource()
    source.ingest(TOPIC_LAPS, _lap(
        7, _id=1, lap_duration=80.0,
        segments_sector_1=[2049, "2051", None, 2048, True, 3.5, 2064.0],
    ))
    driver = source.snapshot().drivers[0]
    assert driver.segments_sector_1 == [2049, 0, 0, 2048, 0, 0, 2064]


def test_segments_default_to_an_empty_list_when_absent() -> None:
    source = OpenF1LiveSource()
    source.ingest(TOPIC_LAPS, _lap(7, _id=1, lap_duration=80.0))
    driver = source.snapshot().drivers[0]
    assert driver.segments_sector_1 == []
    assert driver.segments_sector_2 == []
    assert driver.segments_sector_3 == []


def test_a_fresh_lap_record_replaces_the_previous_lap_segments() -> None:
    source = OpenF1LiveSource()
    source.ingest(TOPIC_LAPS, _lap(11, _id=1, lap_duration=80.5, segments_sector_1=[2051] * 8))
    assert source.snapshot().drivers[0].segments_sector_1 == [2051] * 8

    source.ingest(TOPIC_LAPS, _lap(12, _id=2, segments_sector_1=[2048]))
    assert source.snapshot().drivers[0].segments_sector_1 == [2048], "reset, not appended"


def test_segments_grow_in_place_as_the_car_crosses_each_mini_sector() -> None:
    """The upsert on (driver_number, lap_number) revises the lap in place, so a
    revision carrying a longer array must not be merged element-wise."""
    source = OpenF1LiveSource()
    source.ingest(TOPIC_LAPS, _lap(12, _id=1, segments_sector_1=[2049, 2049]))
    source.ingest(TOPIC_LAPS, _lap(12, _id=2, segments_sector_1=[2049, 2049, 2051, 2048]))

    assert source.snapshot().drivers[0].segments_sector_1 == [2049, 2049, 2051, 2048]


def test_segment_array_lengths_are_not_assumed_to_match_each_other() -> None:
    source = OpenF1LiveSource()
    source.ingest(TOPIC_LAPS, _lap(
        9, _id=1, lap_duration=80.0,
        segments_sector_1=[2049] * 8, segments_sector_2=[2049] * 7, segments_sector_3=[2049] * 9,
    ))
    driver = source.snapshot().drivers[0]
    assert (len(driver.segments_sector_1), len(driver.segments_sector_2), len(driver.segments_sector_3)) == (8, 7, 9)


def test_normalise_segments_handles_anything_openf1_might_send() -> None:
    assert normalise_segments([2048, 2049, 2051, 2064, 0]) == [2048, 2049, 2051, 2064, 0]
    assert normalise_segments([2049.0]) == [2049], "a JSON decoder may hand back a whole float"
    assert normalise_segments([]) == []
    assert normalise_segments(None) == []
    assert normalise_segments("2049") == [], "not a list at all"
    assert normalise_segments({"a": 1}) == []


# -- best sectors and theoretical lap ---------------------------------------------


def _ingest_lap(source: OpenF1LiveSource, driver: int, lap_number: int, message_id: int, **fields: object) -> None:
    source.ingest(TOPIC_LAPS, {"session_key": 1, "driver_number": driver, "lap_number": lap_number,
                               "_id": message_id, **fields})


def test_best_sector_excludes_a_faster_time_set_on_a_pit_out_lap() -> None:
    source = OpenF1LiveSource()
    _ingest_lap(source, 16, 1, 1, lap_duration=90.0, duration_sector_2=24.0, is_pit_out_lap=True)
    _ingest_lap(source, 16, 2, 2, lap_duration=80.0, duration_sector_2=26.0, is_pit_out_lap=False)

    driver = source.snapshot().drivers[0]
    assert driver.best_sector_2 == 26.0, "the pit-out lap's faster time must not count"


def test_best_sector_excludes_non_positive_readings() -> None:
    source = OpenF1LiveSource()
    _ingest_lap(source, 16, 1, 1, lap_duration=80.0, duration_sector_1=0)
    _ingest_lap(source, 16, 2, 2, lap_duration=80.0, duration_sector_1=26.5)

    assert source.snapshot().drivers[0].best_sector_1 == 26.5


def test_theoretical_lap_is_the_sum_of_the_three_best_sectors() -> None:
    source = OpenF1LiveSource()
    _ingest_lap(source, 16, 1, 1, lap_duration=80.0,
                duration_sector_1=26.0, duration_sector_2=28.0, duration_sector_3=26.5)
    _ingest_lap(source, 16, 2, 2, lap_duration=79.0,
                duration_sector_1=25.5, duration_sector_2=27.5, duration_sector_3=27.0)

    driver = source.snapshot().drivers[0]
    assert driver.best_sector_1 == 25.5
    assert driver.best_sector_2 == 27.5
    assert driver.best_sector_3 == 26.5
    assert driver.theoretical_lap == pytest.approx(25.5 + 27.5 + 26.5)


def test_theoretical_lap_is_none_when_sector_3_never_arrives() -> None:
    source = OpenF1LiveSource()
    _ingest_lap(source, 16, 1, 1, lap_duration=80.0, duration_sector_1=26.0, duration_sector_2=28.0)
    _ingest_lap(source, 16, 2, 2, lap_duration=79.0, duration_sector_1=25.5, duration_sector_2=27.5)

    driver = source.snapshot().drivers[0]
    assert driver.best_sector_1 is not None and driver.best_sector_2 is not None
    assert driver.best_sector_3 is None
    assert driver.theoretical_lap is None


def test_session_best_sectors_is_the_minimum_across_drivers() -> None:
    source = OpenF1LiveSource()
    _ingest_lap(source, 16, 1, 1, lap_duration=80.0,
                duration_sector_1=26.0, duration_sector_2=27.0, duration_sector_3=26.5)
    _ingest_lap(source, 4, 1, 2, lap_duration=81.0,
                duration_sector_1=25.5, duration_sector_2=28.0, duration_sector_3=26.0)

    state = source.snapshot()
    assert state.session_best_sectors == [25.5, 27.0, 26.0]


# -- practice/qualifying gap fallback ----------------------------------------------


def test_practice_gap_and_interval_computed_from_best_lap_when_intervals_are_empty() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/sessions", {"session_key": 1, "session_type": "Practice"})
    _ingest_lap(source, 1, 1, 1, lap_duration=80.0)  # leader
    _ingest_lap(source, 2, 1, 2, lap_duration=80.5)
    _ingest_lap(source, 3, 1, 3, lap_duration=81.2)
    source.ingest("v1/position", {"session_key": 1, "driver_number": 1, "position": 1})
    source.ingest("v1/position", {"session_key": 1, "driver_number": 2, "position": 2})
    source.ingest("v1/position", {"session_key": 1, "driver_number": 3, "position": 3})

    by_number = {d.driver_number: d for d in source.snapshot().drivers}
    assert by_number[1].gap_to_leader == 0.0
    assert by_number[1].interval_ahead is None
    assert by_number[2].gap_to_leader == pytest.approx(0.5)
    assert by_number[2].interval_ahead == pytest.approx(0.5)
    assert by_number[3].gap_to_leader == pytest.approx(1.2)
    assert by_number[3].interval_ahead == pytest.approx(0.7)


def test_practice_gap_is_none_for_a_driver_with_no_best_lap() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/sessions", {"session_key": 1, "session_type": "Practice"})
    _ingest_lap(source, 1, 1, 1, lap_duration=80.0)
    source.ingest("v1/position", {"session_key": 1, "driver_number": 1, "position": 1})
    source.ingest("v1/position", {"session_key": 1, "driver_number": 9, "position": 2})

    by_number = {d.driver_number: d for d in source.snapshot().drivers}
    assert by_number[9].gap_to_leader is None
    assert by_number[9].interval_ahead is None


def test_practice_gap_fallback_never_runs_once_real_intervals_are_flowing() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/sessions", {"session_key": 1, "session_type": "Practice"})
    _ingest_lap(source, 1, 1, 1, lap_duration=80.0)
    _ingest_lap(source, 2, 1, 2, lap_duration=80.5)
    source.ingest("v1/intervals", {"session_key": 1, "driver_number": 2,
                                   "gap_to_leader": 9.99, "interval": 9.99, "date": "2026-01-01T00:00:00Z"})

    driver_2 = next(d for d in source.snapshot().drivers if d.driver_number == 2)
    assert driver_2.gap_to_leader == 9.99, "the real value, not the best-lap approximation"


def test_race_gap_is_left_untouched_even_with_no_intervals_yet() -> None:
    """A race can start before intervals arrive too - the fallback is keyed on
    session_type alone, and must never fire for a race."""
    source = OpenF1LiveSource()
    source.ingest("v1/sessions", {"session_key": 1, "session_type": "Race"})
    _ingest_lap(source, 1, 1, 1, lap_duration=80.0)
    _ingest_lap(source, 2, 1, 2, lap_duration=80.5)

    by_number = {d.driver_number: d for d in source.snapshot().drivers}
    assert by_number[1].gap_to_leader is None
    assert by_number[2].gap_to_leader is None


# -- sector flags and safety car ---------------------------------------------------


def _rc(**fields: object) -> dict[str, object]:
    return {"session_key": 1, **fields}


def test_a_sector_yellow_is_set_and_appears_in_sector_flags() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/laps", {"session_key": 1, "driver_number": 1, "lap_number": 1, "_id": 1})
    source.ingest("v1/race_control", _rc(date="2026-01-01T00:00:00Z", category="Flag",
                                         scope="Sector", sector=7, flag="YELLOW",
                                         message="YELLOW IN TRACK SECTOR 7"))

    flags = source.snapshot().sector_flags
    assert len(flags) == 1
    assert flags[0].sector == 7
    assert flags[0].flag == "YELLOW"
    assert flags[0].since == "2026-01-01T00:00:00Z"


def test_a_sector_is_cleared_by_its_own_clear() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/laps", {"session_key": 1, "driver_number": 1, "lap_number": 1, "_id": 1})
    source.ingest("v1/race_control", _rc(date="2026-01-01T00:00:00Z", category="Flag",
                                         scope="Sector", sector=7, flag="DOUBLE YELLOW"))
    source.ingest("v1/race_control", _rc(date="2026-01-01T00:00:10Z", category="Flag",
                                         scope="Sector", sector=7, flag="CLEAR"))

    assert source.snapshot().sector_flags == []


def test_other_sectors_stay_yellow_when_one_clears() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/laps", {"session_key": 1, "driver_number": 1, "lap_number": 1, "_id": 1})
    source.ingest("v1/race_control", _rc(date="2026-01-01T00:00:00Z", category="Flag",
                                         scope="Sector", sector=3, flag="YELLOW"))
    source.ingest("v1/race_control", _rc(date="2026-01-01T00:00:01Z", category="Flag",
                                         scope="Sector", sector=7, flag="YELLOW"))
    source.ingest("v1/race_control", _rc(date="2026-01-01T00:00:10Z", category="Flag",
                                         scope="Sector", sector=3, flag="CLEAR"))

    flags = source.snapshot().sector_flags
    assert [f.sector for f in flags] == [7]


def test_a_track_scope_green_clears_every_sector() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/laps", {"session_key": 1, "driver_number": 1, "lap_number": 1, "_id": 1})
    source.ingest("v1/race_control", _rc(date="2026-01-01T00:00:00Z", category="Flag",
                                         scope="Sector", sector=3, flag="YELLOW"))
    source.ingest("v1/race_control", _rc(date="2026-01-01T00:00:01Z", category="Flag",
                                         scope="Sector", sector=9, flag="DOUBLE YELLOW"))
    source.ingest("v1/race_control", _rc(date="2026-01-01T00:00:10Z", category="Flag",
                                         scope="Track", flag="GREEN"))

    assert source.snapshot().sector_flags == []
    assert source.snapshot().track_flag is None


def test_sector_flags_are_ordered_by_sector_number() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/laps", {"session_key": 1, "driver_number": 1, "lap_number": 1, "_id": 1})
    source.ingest("v1/race_control", _rc(date="2026-01-01T00:00:00Z", category="Flag",
                                         scope="Sector", sector=9, flag="YELLOW"))
    source.ingest("v1/race_control", _rc(date="2026-01-01T00:00:01Z", category="Flag",
                                         scope="Sector", sector=2, flag="YELLOW"))

    assert [f.sector for f in source.snapshot().sector_flags] == [2, 9]


def test_vsc_deployed_then_ending() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/laps", {"session_key": 1, "driver_number": 1, "lap_number": 1, "_id": 1})
    source.ingest("v1/race_control", _rc(date="2026-01-01T00:00:00Z", category="SafetyCar",
                                         message="VIRTUAL SAFETY CAR DEPLOYED"))
    assert source.snapshot().safety_car == "VSC DEPLOYED"

    source.ingest("v1/race_control", _rc(date="2026-01-01T00:02:00Z", category="SafetyCar",
                                         message="VIRTUAL SAFETY CAR ENDING"))
    assert source.snapshot().safety_car == "VSC ENDING"


def test_full_safety_car_deployed_is_not_confused_with_vsc() -> None:
    """"SAFETY CAR DEPLOYED" is a substring of "VIRTUAL SAFETY CAR DEPLOYED" -
    the two must not be conflated in either direction."""
    source = OpenF1LiveSource()
    source.ingest("v1/laps", {"session_key": 1, "driver_number": 1, "lap_number": 1, "_id": 1})
    source.ingest("v1/race_control", _rc(date="2026-01-01T00:00:00Z", category="SafetyCar",
                                         message="SAFETY CAR DEPLOYED"))
    assert source.snapshot().safety_car == "SC DEPLOYED"


def test_safety_car_in_this_lap() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/laps", {"session_key": 1, "driver_number": 1, "lap_number": 1, "_id": 1})
    source.ingest("v1/race_control", _rc(date="2026-01-01T00:00:00Z", category="SafetyCar",
                                         message="SAFETY CAR IN THIS LAP"))
    assert source.snapshot().safety_car == "SC IN THIS LAP"


def test_safety_car_cleared_by_a_later_track_green() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/laps", {"session_key": 1, "driver_number": 1, "lap_number": 1, "_id": 1})
    source.ingest("v1/race_control", _rc(date="2026-01-01T00:00:00Z", category="SafetyCar",
                                         message="SAFETY CAR DEPLOYED"))
    source.ingest("v1/race_control", _rc(date="2026-01-01T00:05:00Z", category="Flag",
                                         scope="Track", flag="GREEN"))

    assert source.snapshot().safety_car is None


def test_safety_car_not_cleared_by_an_earlier_track_green() -> None:
    """Only a green that arrives *after* the safety car record may clear it."""
    source = OpenF1LiveSource()
    source.ingest("v1/laps", {"session_key": 1, "driver_number": 1, "lap_number": 1, "_id": 1})
    source.ingest("v1/race_control", _rc(date="2026-01-01T00:00:00Z", category="Flag",
                                         scope="Track", flag="GREEN"))
    source.ingest("v1/race_control", _rc(date="2026-01-01T00:05:00Z", category="SafetyCar",
                                         message="SAFETY CAR DEPLOYED"))

    assert source.snapshot().safety_car == "SC DEPLOYED"


# -- sector leaders -----------------------------------------------------------------


def _driver_lap(driver: int, s1: float | None, s2: float | None, s3: float | None) -> dict[str, object]:
    fields: dict[str, object] = {"lap_duration": 80.0}
    if s1 is not None:
        fields["duration_sector_1"] = s1
    if s2 is not None:
        fields["duration_sector_2"] = s2
    if s3 is not None:
        fields["duration_sector_3"] = s3
    return {"session_key": 1, "driver_number": driver, "lap_number": 1, "_id": driver, **fields}


def test_sector_leaders_ranks_ascending_and_caps_at_three_of_four() -> None:
    source = OpenF1LiveSource()
    source.ingest(TOPIC_LAPS, _driver_lap(1, 27.0, 28.0, 26.0))
    source.ingest(TOPIC_LAPS, _driver_lap(2, 26.5, 27.5, 26.5))
    source.ingest(TOPIC_LAPS, _driver_lap(3, 27.5, 27.0, 25.5))
    source.ingest(TOPIC_LAPS, _driver_lap(4, 28.0, 29.0, 27.0))

    leaders = source.snapshot().sector_leaders
    assert len(leaders) == 3
    s1, s2, s3 = leaders
    assert [entry.driver_number for entry in s1] == [2, 1, 3], "ascending, capped at 3 of 4"
    assert [entry.time for entry in s1] == [26.5, 27.0, 27.5]
    assert [entry.driver_number for entry in s2] == [3, 2, 1]
    assert [entry.driver_number for entry in s3] == [3, 1, 2]


def test_a_driver_with_no_sector_2_is_absent_from_that_list_only() -> None:
    source = OpenF1LiveSource()
    source.ingest(TOPIC_LAPS, _driver_lap(1, 27.0, None, 26.0))
    source.ingest(TOPIC_LAPS, _driver_lap(2, 26.5, 27.5, 26.5))

    leaders = source.snapshot().sector_leaders
    assert [e.driver_number for e in leaders[0]] == [2, 1]
    assert [e.driver_number for e in leaders[1]] == [2], "driver 1 has no sector 2 time"
    assert [e.driver_number for e in leaders[2]] == [1, 2]


def test_sector_leaders_carry_acronym_and_team_colour() -> None:
    source = OpenF1LiveSource()
    source.ingest("v1/drivers", {"session_key": 1, "driver_number": 1,
                                 "name_acronym": "LEC", "team_colour": "ED1131"})
    source.ingest(TOPIC_LAPS, _driver_lap(1, 27.0, 28.0, 26.0))

    entry = source.snapshot().sector_leaders[0][0]
    assert entry.name_acronym == "LEC"
    assert entry.team_colour == "ED1131"
