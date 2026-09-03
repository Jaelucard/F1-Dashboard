"""replay.py: streaming reader, catalog, and the player that re-feeds the adapter.

Recordings for these tests are written with the recorder's own ``RawMessage``
line format, so the reader can never drift from what the recorder writes.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from app.adapters.openf1_live import OpenF1LiveSource
from app.config import Settings
from app.recorder import RawMessage, safe_component
from app.replay import (
    RecordingCatalog,
    RecordingReader,
    ReplayError,
    ReplayPlayer,
    parse_line,
    resolve_session_dir,
)

T0 = datetime(2026, 9, 4, 11, 30, 0, tzinfo=timezone.utc)
KEY = 9001

Message = tuple[str, dict[str, Any], float]  # topic, payload, seconds after T0


def write_recording(root: Path, key: int, messages: list[Message]) -> Path:
    directory = root / str(key)
    directory.mkdir(parents=True, exist_ok=True)
    for topic, payload, offset in messages:
        line = RawMessage(received_at=T0 + timedelta(seconds=offset), topic=topic, payload=payload).to_line()
        with (directory / f"{safe_component(topic, 'unknown_topic')}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    return directory


def session_messages(key: int = KEY, drivers: int = 3) -> list[Message]:
    """A small but realistic session: entry list, then positions ticking along."""
    out: list[Message] = [
        ("v1/sessions", {"session_key": key, "session_name": "Practice 1", "circuit_short_name": "Monza"}, 0.0),
    ]
    for n in range(1, drivers + 1):
        out.append(("v1/drivers", {"driver_number": n, "name_acronym": f"D{n:02d}", "session_key": key}, 0.5))
    tick = 0
    for second in range(1, 11):
        for n in range(1, drivers + 1):
            tick += 1
            out.append(
                ("v1/position", {"driver_number": n, "position": ((n + second) % drivers) + 1,
                                 "date": (T0 + timedelta(seconds=second)).isoformat(),
                                 "session_key": key, "_id": tick}, float(second) + n * 0.01)
            )
    return out


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        openf1_username=SecretStr("test-user@example.invalid"),
        openf1_password=SecretStr("test-password-placeholder"),
        live_mode=False,
        recordings_dir=tmp_path,
    )


def wait_for(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


# -- parse_line / reader ------------------------------------------------------


def test_parse_line_recovers_the_recorder_format() -> None:
    line = RawMessage(received_at=T0, topic="v1/laps", payload={"a": 1}).to_line()
    message = parse_line(line)
    assert message is not None
    assert (message.received_at, message.topic, message.payload) == (T0, "v1/laps", {"a": 1})


@pytest.mark.parametrize(
    "line",
    ["not json", "[1, 2]", '{"topic": "v1/laps"}', '{"received_at": "yesterday", "topic": "v1/laps"}',
     '{"received_at": "2026-09-04T11:30:00+00:00"}'],
)
def test_parse_line_rejects_what_it_cannot_time(line: str) -> None:
    assert parse_line(line) is None


def test_reader_merges_topic_files_by_received_at(tmp_path: Path) -> None:
    directory = write_recording(tmp_path, KEY, [
        ("v1/laps", {"n": 1}, 0.0), ("v1/position", {"n": 2}, 0.1), ("v1/laps", {"n": 3}, 0.2),
        ("v1/intervals", {"n": 4}, 0.15), ("v1/position", {"n": 5}, 0.3),
    ])
    reader = RecordingReader(directory)
    order = [(m.topic, m.payload["n"]) for m in reader]
    assert order == [("v1/laps", 1), ("v1/position", 2), ("v1/intervals", 4), ("v1/laps", 3), ("v1/position", 5)]
    assert reader.skipped_lines == 0


def test_reader_skips_and_counts_bad_lines(tmp_path: Path) -> None:
    directory = write_recording(tmp_path, KEY, [("v1/laps", {"n": 1}, 0.0), ("v1/laps", {"n": 2}, 1.0)])
    path = directory / "v1_laps.jsonl"
    path.write_text(path.read_text() + "garbage\n\n[1,2]\n" + '{"received_at": "x", "topic": "v1/laps"}\n')
    reader = RecordingReader(directory)
    assert [m.payload["n"] for m in reader] == [1, 2]
    assert reader.skipped_lines == 3, "blank lines are not counted; unparseable ones are"


def test_reader_reset_starts_over(tmp_path: Path) -> None:
    directory = write_recording(tmp_path, KEY, [("v1/laps", {"n": 1}, 0.0), ("v1/laps", {"n": 2}, 1.0)])
    reader = RecordingReader(directory)
    assert [m.payload["n"] for m in reader] == [1, 2]
    reader.reset()
    assert [m.payload["n"] for m in reader] == [1, 2]
    reader.close()


def test_reader_on_an_empty_directory_yields_nothing(tmp_path: Path) -> None:
    (tmp_path / "1").mkdir()
    assert list(RecordingReader(tmp_path / "1")) == []


# -- catalog and path safety --------------------------------------------------


def test_catalog_lists_digit_directories_only(tmp_path: Path) -> None:
    write_recording(tmp_path, 11, [("v1/laps", {"session_key": 11}, 0.0)])
    write_recording(tmp_path, 12, [("v1/laps", {"session_key": 12}, 0.0)])
    (tmp_path / "_no_session_key").mkdir()
    (tmp_path / "notes.txt").write_text("x")
    keys = [s.session_key for s in RecordingCatalog(tmp_path).sessions()]
    assert keys == [12, 11], "newest first"


def test_catalog_summary_reads_only_the_ends_of_each_file(tmp_path: Path) -> None:
    write_recording(tmp_path, KEY, session_messages())
    summary = RecordingCatalog(tmp_path).summary(KEY)
    assert summary.start == T0.isoformat(timespec="milliseconds")
    assert summary.end == (T0 + timedelta(seconds=10.03)).isoformat(timespec="milliseconds")
    assert set(summary.topics) == {"v1/sessions", "v1/drivers", "v1/position"}
    assert summary.size_bytes == sum(summary.topics.values())
    assert summary.session_name == "Practice 1" and summary.circuit_short_name == "Monza"


def test_catalog_on_a_missing_root_is_empty(tmp_path: Path) -> None:
    assert RecordingCatalog(tmp_path / "nope").sessions() == []


@pytest.mark.parametrize("key", [-1, 10**12, True, 5])
def test_resolve_session_dir_rejects_bad_or_unknown_keys(tmp_path: Path, key: Any) -> None:
    with pytest.raises(ReplayError) as excinfo:
        resolve_session_dir(tmp_path, key)
    assert str(tmp_path) not in str(excinfo.value)


def test_resolve_session_dir_rejects_a_symlink_out_of_root(tmp_path: Path) -> None:
    root = tmp_path / "recordings"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, root / "77")
    with pytest.raises(ReplayError):
        resolve_session_dir(root, 77)


def test_resolve_session_dir_accepts_a_real_recording(tmp_path: Path) -> None:
    write_recording(tmp_path, KEY, [("v1/laps", {"session_key": KEY}, 0.0)])
    assert resolve_session_dir(tmp_path, KEY) == tmp_path / str(KEY)


# -- player -------------------------------------------------------------------


def make_player(tmp_path: Path, **kwargs: Any) -> tuple[ReplayPlayer, list[float]]:
    sleeps: list[float] = []
    kwargs.setdefault("sleep", sleeps.append)
    return ReplayPlayer(make_settings(tmp_path), KEY, **kwargs), sleeps


def test_player_starts_loaded_at_the_beginning(tmp_path: Path) -> None:
    write_recording(tmp_path, KEY, session_messages())
    player, _ = make_player(tmp_path)
    try:
        info = player.info()
        assert info.state == "loaded" and info.progress == 0.0 and info.position is None
        assert info.session_key == KEY and info.speed == 1.0
        snapshot = player.snapshot()
        assert snapshot.mode == "replay" and snapshot.recorder is None and snapshot.feed is None
        assert snapshot.replay is not None and snapshot.replay.state == "loaded"
    finally:
        player.stop()


def test_player_plays_to_the_end_pacing_by_received_at(tmp_path: Path) -> None:
    write_recording(tmp_path, KEY, session_messages(drivers=2))
    player, sleeps = make_player(tmp_path)
    try:
        player.play()
        assert wait_for(lambda: player.state == "finished")
        info = player.info()
        assert info.messages_replayed == 1 + 2 + 20
        assert info.progress == 1.0
        assert player.snapshot().drivers and player.snapshot().session is not None
        # Gaps between consecutive messages, at speed 1.
        assert sleeps[0] == pytest.approx(0.5)      # sessions -> first driver
        assert sleeps[2] == pytest.approx(0.51)     # last driver (0.5) -> first position (1.01)
        assert sleeps[3] == pytest.approx(0.01)
    finally:
        player.stop()


def test_player_clamps_long_gaps_and_scales_by_speed(tmp_path: Path) -> None:
    write_recording(tmp_path, KEY, [
        ("v1/laps", {"session_key": KEY, "driver_number": 1, "lap_number": 1}, 0.0),
        ("v1/laps", {"session_key": KEY, "driver_number": 1, "lap_number": 2}, 1.0),
        ("v1/laps", {"session_key": KEY, "driver_number": 1, "lap_number": 3}, 61.0),  # red flag
    ])
    player, sleeps = make_player(tmp_path)
    try:
        player.set_speed(2.0)
        player.play()
        assert wait_for(lambda: player.state == "finished")
        assert sleeps == [pytest.approx(0.5), pytest.approx(5.0)]
    finally:
        player.stop()


def test_player_pause_and_resume(tmp_path: Path) -> None:
    write_recording(tmp_path, KEY, session_messages())
    gate = {"open": False}

    def slow_sleep(seconds: float) -> None:
        while not gate["open"]:
            time.sleep(0.005)

    player, _ = make_player(tmp_path, sleep=slow_sleep)
    try:
        player.play()
        assert wait_for(lambda: player.info().messages_replayed >= 1)
        player.pause()
        assert player.state == "paused"
        gate["open"] = True
        time.sleep(0.05)
        seen = player.info().messages_replayed
        time.sleep(0.05)
        assert player.info().messages_replayed <= seen + 1, "paused: at most the held message applies"
        player.play()
        assert wait_for(lambda: player.state == "finished")
    finally:
        player.stop()


@pytest.mark.parametrize("speed", [0.1, 51.0, 0.0])
def test_player_rejects_speeds_outside_the_range(tmp_path: Path, speed: float) -> None:
    write_recording(tmp_path, KEY, session_messages())
    player, _ = make_player(tmp_path)
    try:
        with pytest.raises(ValueError):
            player.set_speed(speed)
    finally:
        player.stop()


def test_forward_seek_applies_without_sleeping(tmp_path: Path) -> None:
    write_recording(tmp_path, KEY, session_messages())
    player, sleeps = make_player(tmp_path)
    try:
        player.seek(fraction=0.5)
        assert wait_for(lambda: player.state == "paused")
        assert sleeps == []
        info = player.info()
        assert info.position == (T0 + timedelta(seconds=5.015)).isoformat(timespec="milliseconds")
        assert 0.45 < info.progress < 0.55
        # Seconds 1-4 complete (3 each) plus the one message at 5.01; 5.02 and 5.03 are past the target.
        assert info.messages_replayed == 1 + 3 + 4 * 3 + 1
    finally:
        player.stop()


def test_backward_seek_matches_a_fresh_run_to_the_same_point(tmp_path: Path) -> None:
    write_recording(tmp_path, KEY, session_messages())
    player, _ = make_player(tmp_path)
    try:
        player.seek(fraction=0.8)
        assert wait_for(lambda: player.state == "paused")
        player.seek(position=T0 + timedelta(seconds=3.5))
        assert wait_for(lambda: player.state == "paused")

        expected = OpenF1LiveSource()
        for message in RecordingReader(tmp_path / str(KEY)):
            if message.received_at <= T0 + timedelta(seconds=3.5):
                expected.ingest(message.topic, message.payload)
        want = [(d.driver_number, d.position) for d in expected.snapshot().drivers]
        got = [(d.driver_number, d.position) for d in player.snapshot().drivers]
        assert got == want
        assert player.info().messages_replayed == 1 + 3 + 3 * 3
    finally:
        player.stop()


def test_seek_is_clamped_to_the_recording(tmp_path: Path) -> None:
    write_recording(tmp_path, KEY, session_messages())
    player, _ = make_player(tmp_path)
    try:
        player.seek(position=T0 + timedelta(days=1))
        assert wait_for(lambda: player.state == "finished")
        assert player.info().progress == 1.0
        player.seek(position=T0 - timedelta(days=1))
        assert wait_for(lambda: player.state == "paused")
        assert player.info().progress == 0.0
    finally:
        player.stop()


def test_play_after_finished_restarts(tmp_path: Path) -> None:
    write_recording(tmp_path, KEY, session_messages(drivers=1))
    player, _ = make_player(tmp_path)
    try:
        player.play()
        assert wait_for(lambda: player.state == "finished")
        first = player.info().messages_replayed
        player.play()
        assert wait_for(lambda: player.state == "finished")
        assert player.info().messages_replayed == first
    finally:
        player.stop()


def test_ingest_errors_are_counted_and_do_not_stop_the_replay(tmp_path: Path) -> None:
    write_recording(tmp_path, KEY, session_messages(drivers=1))

    class Exploding(OpenF1LiveSource):
        def ingest(self, topic: str, payload: Any) -> bool:
            if topic == "v1/position":
                raise RuntimeError("boom")
            return super().ingest(topic, payload)

    player, _ = make_player(tmp_path, adapter_factory=Exploding)
    try:
        player.play()
        assert wait_for(lambda: player.state == "finished")
        assert player.info().ingest_errors == 10
        assert player.info().messages_replayed == 2
    finally:
        player.stop()


def test_player_refuses_an_unknown_session(tmp_path: Path) -> None:
    with pytest.raises(ReplayError) as excinfo:
        ReplayPlayer(make_settings(tmp_path), 4242)
    assert str(tmp_path) not in str(excinfo.value)


def test_info_never_contains_a_path(tmp_path: Path) -> None:
    write_recording(tmp_path, KEY, session_messages())
    player, _ = make_player(tmp_path)
    try:
        assert str(tmp_path) not in json.dumps(player.info().model_dump())
    finally:
        player.stop()
