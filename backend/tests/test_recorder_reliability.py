"""recorder.py: failure modes that must be visible, not silent.

Disk full, permissions, lost messages, a stale token, a slow adapter: each has
to show up in health and logs, and none of them may stop the recorder or make
it claim a complete capture it does not have.
"""

from __future__ import annotations

import errno
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from app.auth import AuthError
from app.config import Settings
from app.recorder import (
    FanOutQueue,
    IdSequenceTracker,
    JsonlWriter,
    RawMessage,
    RawRecorder,
    RecordingError,
    run_standalone,
)


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        openf1_username=SecretStr("test-user@example.invalid"),
        openf1_password=SecretStr("test-password-placeholder"),
        live_mode=True,
        recordings_dir=tmp_path,
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


class FakeTokenProvider:
    def __init__(self, *, fail: bool = False) -> None:
        self.username = "test-user@example.invalid"
        self.calls: list[bool] = []
        self.fail = fail
        self.current_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    def get_token(self, *, force_refresh: bool = False) -> str:
        self.calls.append(force_refresh)
        if self.fail:
            raise AuthError("token endpoint rejected the credentials (HTTP 401)", kind="credentials")
        return f"test-token-placeholder-{len(self.calls)}"

    def seconds_until_refresh(self) -> float:
        return 3600.0

    def close(self) -> None:
        pass


class FakeClient:
    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.username: str | None = None
        self.password: str | None = None
        self.subscriptions: list[tuple[str, int]] = []
        self.disconnected = False
        self.on_connect: Any = None
        self.on_message: Any = None
        self.on_disconnect: Any = None
        self.on_subscribe: Any = None

    def username_pw_set(self, username: str, password: str) -> None:
        self.username, self.password = username, password

    def connect(self, host: str, port: int, keepalive: int = 60) -> None:
        pass

    def loop_start(self) -> None:
        if self.on_connect:
            self.on_connect(self, None, {}, 5 if self.refuse else 0, None)

    def loop_stop(self) -> None:
        pass

    def disconnect(self) -> None:
        self.disconnected = True

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscriptions.append((topic, qos))


def make_recorder(tmp_path: Path, **kwargs: Any) -> tuple[RawRecorder, FakeTokenProvider]:
    tokens = kwargs.pop("tokens", None) or FakeTokenProvider()
    settings = kwargs.pop("settings", None) or make_settings(tmp_path)
    recorder = RawRecorder(settings, tokens, **kwargs)  # type: ignore[arg-type]
    return recorder, tokens


def wait_for(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# -- standalone safety --------------------------------------------------------


def test_standalone_recorder_refuses_to_run_when_live_mode_is_off(tmp_path: Path) -> None:
    built: list[Any] = []

    def factory(settings: Settings) -> RawRecorder:
        built.append(settings)
        raise AssertionError("must not build a recorder (and so never request a token)")

    code = run_standalone(make_settings(tmp_path, live_mode=False), recorder_factory=factory,
                          stop_event=threading.Event())
    assert code != 0
    assert built == []


def test_standalone_recorder_runs_with_an_explicit_override(tmp_path: Path) -> None:
    stop = threading.Event()
    stop.set()  # return immediately once started
    started: list[bool] = []

    class Stub:
        stats = RawRecorder(make_settings(tmp_path), FakeTokenProvider()).stats  # type: ignore[arg-type]

        def start(self) -> None:
            started.append(True)

        def stop(self) -> None:
            pass

    code = run_standalone(make_settings(tmp_path, live_mode=False), force=True,
                          recorder_factory=lambda settings: Stub(), stop_event=stop)  # type: ignore[arg-type,return-value]
    assert code == 0
    assert started == [True]


def test_standalone_recorder_does_not_wait_for_a_token_when_disabled(tmp_path: Path) -> None:
    """Even with credentials present, LIVE_MODE=false must be honoured."""
    code = run_standalone(make_settings(tmp_path, live_mode=False), stop_event=threading.Event())
    assert code == 2


# -- disk failures ------------------------------------------------------------


class Flaky:
    """A file-like object that fails on demand."""

    def __init__(self, error: OSError | None) -> None:
        self.error = error
        self.name = "flaky.jsonl"
        self.lines: list[str] = []
        self.closed = False

    def write(self, text: str) -> int:
        if self.error is not None:
            raise self.error
        self.lines.append(text)
        return len(text)

    def flush(self) -> None:
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self.closed = True


def test_writer_classifies_disk_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writer = JsonlWriter(tmp_path)
    handle = Flaky(OSError(errno.ENOSPC, "No space left on device"))
    monkeypatch.setattr(Path, "open", lambda self, *a, **k: handle)
    with pytest.raises(RecordingError) as excinfo:
        writer.write("1", "v1_laps", "{}")
    assert excinfo.value.kind == "disk_full"
    assert "ENOSPC" in str(excinfo.value) or "space" in str(excinfo.value).lower()
    assert writer.open_files == 0, "a failed handle is discarded so the next write reopens"


def test_writer_classifies_permission_and_mkdir_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writer = JsonlWriter(tmp_path)

    def denied(self: Path, *a: Any, **k: Any) -> None:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(Path, "mkdir", denied)
    with pytest.raises(RecordingError) as excinfo:
        writer.write("1", "v1_laps", "{}")
    assert excinfo.value.kind == "permission"
    assert excinfo.value.stage == "mkdir"


def test_writer_reports_flush_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writer = JsonlWriter(tmp_path)

    class FlushFails(Flaky):
        def write(self, text: str) -> int:
            self.lines.append(text)
            return len(text)

    handle = FlushFails(OSError(errno.EIO, "Input/output error"))
    monkeypatch.setattr(Path, "open", lambda self, *a, **k: handle)
    with pytest.raises(RecordingError) as excinfo:
        writer.write("1", "v1_laps", "{}")
    assert excinfo.value.kind == "io"
    assert excinfo.value.stage == "flush"


def test_a_write_failure_is_counted_surfaced_and_does_not_stop_fan_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    recorder, _ = make_recorder(tmp_path)
    seen: list[RawMessage] = []
    recorder.add_subscriber(seen.append)

    def fail(self: JsonlWriter, *a: Any, **k: Any) -> Path:
        raise RecordingError("disk_full", "write", "no space left on device (ENOSPC)")

    with caplog.at_level(logging.ERROR, logger="app.recorder"):
        with monkeypatch.context() as patch:
            patch.setattr(JsonlWriter, "write", fail)
            for i in range(3):
                recorder.handle_payload("v1/laps", json.dumps({"session_key": 1, "n": i}))

    assert recorder.stats.write_failures == 3
    assert recorder.stats.recording_ok is False
    assert recorder.stats.last_error is not None and "disk_full" in recorder.stats.last_error
    assert recorder.stats.messages_recorded == 0, "an unwritten message is not a recorded one"
    assert len(seen) == 3, "the dashboard still sees the data"
    assert any("disk_full" in r.getMessage() for r in caplog.records)

    health = recorder.health()
    assert health["recording_ok"] is False
    assert health["write_failures"] == 3
    assert health["may_be_incomplete"] is True
    assert health["recording_error"] is not None

    # Disk comes back: writes resume and the flag recovers, the history stays.
    recorder.handle_payload("v1/laps", json.dumps({"session_key": 1, "n": 99}))
    assert recorder.stats.recording_ok is True
    assert recorder.stats.write_failures == 3
    assert recorder.health()["may_be_incomplete"] is True
    assert (tmp_path / "1" / "v1_laps.jsonl").exists()


def test_write_failures_are_logged_at_most_once_per_burst(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """150 messages a second must not become 150 error lines a second."""
    recorder, _ = make_recorder(tmp_path)

    def fail(self: JsonlWriter, *a: Any, **k: Any) -> Path:
        raise RecordingError("disk_full", "write", "no space left on device (ENOSPC)")

    monkeypatch.setattr(JsonlWriter, "write", fail)
    with caplog.at_level(logging.ERROR, logger="app.recorder"):
        for i in range(200):
            recorder.handle_payload("v1/laps", json.dumps({"session_key": 1, "n": i}))
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert 1 <= len(errors) <= 3


def test_health_reports_disk_space(tmp_path: Path) -> None:
    recorder, _ = make_recorder(tmp_path, settings=make_settings(tmp_path, recorder_min_free_bytes=1))
    health = recorder.health()
    assert isinstance(health["disk_free_bytes"], int) and health["disk_free_bytes"] > 0
    assert health["disk_low"] is False

    recorder, _ = make_recorder(tmp_path, settings=make_settings(tmp_path, recorder_min_free_bytes=2**62))
    assert recorder.health()["disk_low"] is True


# -- _id gaps -----------------------------------------------------------------


def test_id_tracker_detects_gaps_duplicates_and_reordering() -> None:
    tracker = IdSequenceTracker()
    for message_id in (1, 2, 3, 6, 6, 5, 9):
        tracker.observe(message_id)
    assert tracker.count == 7
    assert tracker.min_id == 1 and tracker.max_id == 9
    assert tracker.duplicates == 1
    assert tracker.out_of_order == 1
    # ids 1..9 are 9 distinct values; we saw 6 distinct ones -> 3 missing.
    assert tracker.missing == 3


def test_recorder_exposes_id_gaps_per_topic_and_flags_the_recording(tmp_path: Path) -> None:
    recorder, _ = make_recorder(tmp_path)
    for message_id in (10, 11, 14):
        recorder.handle_payload("v1/laps", json.dumps({"session_key": 1, "_id": message_id}))
    recorder.handle_payload("v1/laps", json.dumps({"session_key": 1}))  # no _id: ignored by the tracker
    for message_id in (1, 2, 3):
        recorder.handle_payload("v1/location", json.dumps({"session_key": 1, "_id": message_id}))

    health = recorder.health()
    ids = health["id_tracking"]
    assert ids["per_topic"]["v1/laps"]["missing"] == 2
    assert ids["per_topic"]["v1/location"]["missing"] == 0
    assert health["messages_possibly_lost"] >= 2
    assert health["may_be_incomplete"] is True


def test_a_clean_sequence_is_not_flagged(tmp_path: Path) -> None:
    recorder, _ = make_recorder(tmp_path)
    for message_id in range(1, 50):
        recorder.handle_payload("v1/laps", json.dumps({"session_key": 1, "_id": message_id}))
    health = recorder.health()
    assert health["messages_possibly_lost"] == 0
    assert health["may_be_incomplete"] is False


def test_a_disconnect_after_data_started_flowing_marks_the_recording_incomplete(tmp_path: Path) -> None:
    recorder, _ = make_recorder(tmp_path)
    recorder._on_disconnect(None, None, None, 7, None)  # noqa: SLF001 - before any data: fine
    assert recorder.health()["may_be_incomplete"] is False

    recorder.handle_payload("v1/laps", json.dumps({"session_key": 1, "_id": 1}))
    recorder._on_disconnect(None, None, None, 7, None)  # noqa: SLF001
    health = recorder.health()
    assert health["may_be_incomplete"] is True
    assert health["disconnects_after_data"] == 1


# -- fan-out queue ------------------------------------------------------------


def test_fan_out_queue_preserves_order_and_isolates_subscribers() -> None:
    seen: list[int] = []
    errors: list[str] = []
    queue = FanOutQueue(maxsize=100, on_error=lambda e: errors.append(type(e).__name__))
    queue.add_subscriber(lambda m: seen.append(m.payload["n"]))
    queue.add_subscriber(lambda m: (_ for _ in ()).throw(RuntimeError("adapter bug")))
    queue.start()
    try:
        for i in range(50):
            assert queue.submit(RawMessage(datetime.now(timezone.utc), "t", {"n": i})) is True
        assert queue.drain(timeout=5)
    finally:
        queue.stop()
    assert seen == list(range(50))
    assert errors == ["RuntimeError"] * 50


def test_fan_out_queue_drops_when_full_and_counts_it() -> None:
    release = threading.Event()
    queue = FanOutQueue(maxsize=5)
    queue.add_subscriber(lambda m: release.wait(5))
    queue.start()
    try:
        # One message is taken by the worker and blocks; five fill the queue.
        results = [queue.submit(RawMessage(datetime.now(timezone.utc), "t", {"n": i})) for i in range(10)]
        assert results.count(False) >= 1
        assert queue.dropped == results.count(False)
        assert queue.high_water >= 5
    finally:
        release.set()
        queue.stop()


def test_recorder_fans_out_through_the_queue_when_running(tmp_path: Path) -> None:
    client = FakeClient()
    recorder, _ = make_recorder(tmp_path, client_factory=lambda: client)
    threads: set[str] = set()
    seen: list[RawMessage] = []

    def subscriber(message: RawMessage) -> None:
        threads.add(threading.current_thread().name)
        seen.append(message)

    recorder.add_subscriber(subscriber)
    recorder.start()
    assert wait_for(lambda: client.on_message is not None)

    class Message:
        topic = "v1/laps"
        payload = json.dumps({"session_key": 1, "_id": 1}).encode()

    client.on_message(client, None, Message())
    assert recorder.drain(timeout=5)
    recorder.stop(timeout=5)

    assert len(seen) == 1
    assert threads == {"openf1-fanout"}, "the adapter never runs on the paho network thread"
    assert recorder.health()["fanout_dropped"] == 0
    assert "fanout_queue" in recorder.health()


def test_fan_out_overflow_is_visible_in_health_and_never_blocks_recording(tmp_path: Path) -> None:
    release = threading.Event()
    recorder, _ = make_recorder(tmp_path, settings=make_settings(tmp_path, fanout_queue_size=4))
    recorder.add_subscriber(lambda m: release.wait(5))
    recorder.start_fan_out()
    try:
        for i in range(20):
            recorder.handle_payload("v1/laps", json.dumps({"session_key": 1, "n": i}))
    finally:
        release.set()
        recorder.stop(timeout=5)

    assert recorder.stats.messages_recorded == 20, "every message on disk regardless"
    assert recorder.stats.fanout_dropped > 0
    assert recorder.health()["fanout_dropped"] == recorder.stats.fanout_dropped
    lines = (tmp_path / "1" / "v1_laps.jsonl").read_text().strip().split("\n")
    assert len(lines) == 20


# -- token lifecycle ----------------------------------------------------------


def test_refused_connection_does_not_fetch_a_token_inside_the_callback(tmp_path: Path) -> None:
    client = FakeClient()
    recorder, tokens = make_recorder(tmp_path, client_factory=lambda: client)
    recorder._client = client  # noqa: SLF001
    recorder._on_connect(client, None, {}, 5, None)  # noqa: SLF001 - rc 5 = not authorized

    assert tokens.calls == [], "no HTTP from the paho thread"
    assert recorder.stats.connected is False
    assert recorder.stats.auth_state == "failed"
    assert recorder.needs_token_refresh is True
    assert "not authorized" in (recorder.stats.last_error or "").lower()


def test_supervisor_refreshes_the_token_after_a_refused_connection(tmp_path: Path) -> None:
    clients: list[FakeClient] = []

    def factory() -> FakeClient:
        client = FakeClient(refuse=len(clients) == 0)
        clients.append(client)
        return client

    recorder, tokens = make_recorder(tmp_path, client_factory=factory, retry_delay_seconds=0.01)
    recorder.start()
    assert wait_for(lambda: len(clients) >= 2 and clients[1].subscriptions)
    recorder.stop(timeout=5)

    assert tokens.calls[:2] == [False, True], "second connect forces a fresh token"
    assert clients[1].password == "test-token-placeholder-2"
    assert recorder.stats.auth_state == "authenticated"


def test_auth_failures_are_classified_and_never_echo_secrets(tmp_path: Path) -> None:
    tokens = FakeTokenProvider(fail=True)
    recorder, _ = make_recorder(tmp_path, tokens=tokens, client_factory=FakeClient, retry_delay_seconds=0.01)
    recorder.start()
    assert wait_for(lambda: len(tokens.calls) >= 2)
    recorder.stop(timeout=5)

    assert recorder.stats.auth_state == "failed"
    assert recorder.stats.auth_failures >= 1
    assert "credentials" in (recorder.stats.last_error or "")
    assert "password" not in (recorder.stats.last_error or "")


def test_network_connect_errors_are_classified_as_network(tmp_path: Path) -> None:
    class Failing(FakeClient):
        def connect(self, host: str, port: int, keepalive: int = 60) -> None:
            raise OSError("network is unreachable")

    recorder, _ = make_recorder(tmp_path, client_factory=Failing, retry_delay_seconds=0.01)
    recorder.start()
    assert wait_for(lambda: recorder.stats.connect_failures >= 2)
    recorder.stop(timeout=5)
    assert recorder.stats.last_error_kind == "network"
    assert recorder.stats.auth_state != "failed"


def test_subscribe_qos_comes_from_settings(tmp_path: Path) -> None:
    client = FakeClient()
    recorder, _ = make_recorder(tmp_path, settings=make_settings(tmp_path, mqtt_qos=1), client_factory=lambda: client)
    recorder.start()
    assert wait_for(lambda: bool(client.subscriptions))
    recorder.stop(timeout=5)
    assert client.subscriptions == [("#", 1)]
