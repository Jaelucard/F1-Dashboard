"""recorder.py: the on-disk format, and the guarantee that nothing stops it."""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.recorder import (
    UNKNOWN_SESSION,
    JsonlWriter,
    RawMessage,
    RawRecorder,
    safe_component,
)

ISO_WITH_MILLIS = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00$"
)


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        openf1_username=SecretStr("test-user@example.invalid"),
        openf1_password=SecretStr("test-password-placeholder"),
        live_mode=True,
        recordings_dir=tmp_path,
    )


class FakeTokenProvider:
    """Stands in for TokenProvider without touching the network."""

    def __init__(self, *, lifetime: float = 3600.0) -> None:
        self.username = "test-user@example.invalid"
        self.calls: list[bool] = []
        self.lifetime = lifetime
        self.current_expiry = datetime.now(timezone.utc) + timedelta(seconds=lifetime)

    def get_token(self, *, force_refresh: bool = False) -> str:
        self.calls.append(force_refresh)
        return f"test-token-placeholder-{len(self.calls)}"

    def seconds_until_refresh(self) -> float:
        return self.lifetime

    def close(self) -> None:
        pass


class FakeClient:
    """Minimal stand-in for paho's Client."""

    def __init__(self) -> None:
        self.username: str | None = None
        self.password: str | None = None
        self.connected_to: tuple[str, int] | None = None
        self.subscriptions: list[tuple[str, int]] = []
        self.loop_started = False
        self.disconnected = False
        self.on_connect: Any = None
        self.on_message: Any = None
        self.on_disconnect: Any = None
        self.on_subscribe: Any = None

    def username_pw_set(self, username: str, password: str) -> None:
        self.username, self.password = username, password

    def connect(self, host: str, port: int, keepalive: int = 60) -> None:
        self.connected_to = (host, port)

    def loop_start(self) -> None:
        self.loop_started = True
        # Simulate the broker acknowledging the connection.
        if self.on_connect:
            self.on_connect(self, None, {}, 0, None)

    def loop_stop(self) -> None:
        self.loop_started = False

    def disconnect(self) -> None:
        self.disconnected = True

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscriptions.append((topic, qos))


def make_recorder(tmp_path: Path, **kwargs: Any) -> tuple[RawRecorder, FakeTokenProvider]:
    tokens = FakeTokenProvider()
    recorder = RawRecorder(make_settings(tmp_path), tokens, **kwargs)  # type: ignore[arg-type]
    return recorder, tokens


# -- the line format --------------------------------------------------------


def test_line_format_has_exactly_the_three_required_fields() -> None:
    message = RawMessage(
        received_at=datetime(2026, 9, 4, 11, 30, 15, 123456, tzinfo=timezone.utc),
        topic="v1/laps",
        payload={"driver_number": 1, "lap_duration": 80.123, "_key": "abc", "_id": 42},
    )
    line = message.to_line()

    assert "\n" not in line, "one JSON object per line"
    parsed = json.loads(line)
    assert set(parsed) == {"received_at", "topic", "payload"}
    assert parsed["topic"] == "v1/laps"
    assert parsed["received_at"] == "2026-09-04T11:30:15.123+00:00"
    assert ISO_WITH_MILLIS.match(parsed["received_at"]), "UTC ISO 8601 with milliseconds"
    # _id and _key survive untouched - replay ordering depends on them.
    assert parsed["payload"]["_id"] == 42
    assert parsed["payload"]["_key"] == "abc"


def test_received_at_is_always_utc_with_milliseconds(tmp_path: Path) -> None:
    recorder, _ = make_recorder(tmp_path)
    message = recorder.handle_payload("v1/laps", json.dumps({"session_key": 1}))
    parsed = json.loads(message.to_line())
    assert ISO_WITH_MILLIS.match(parsed["received_at"])
    assert message.received_at.tzinfo == timezone.utc


# -- file layout ------------------------------------------------------------


def test_writes_to_session_key_and_topic_path(tmp_path: Path) -> None:
    recorder, _ = make_recorder(tmp_path)
    recorder.handle_payload("v1/laps", json.dumps({"session_key": 9999, "lap_number": 1}))

    path = tmp_path / "9999" / "v1_laps.jsonl"
    assert path.exists(), f"expected {path}, found {list(tmp_path.rglob('*'))}"
    assert json.loads(path.read_text())["payload"]["lap_number"] == 1


def test_messages_append_rather_than_overwrite(tmp_path: Path) -> None:
    recorder, _ = make_recorder(tmp_path)
    for lap in range(1, 6):
        recorder.handle_payload("v1/laps", json.dumps({"session_key": 7, "lap_number": lap}))

    lines = (tmp_path / "7" / "v1_laps.jsonl").read_text().strip().split("\n")
    assert len(lines) == 5
    assert [json.loads(line)["payload"]["lap_number"] for line in lines] == [1, 2, 3, 4, 5]


def test_each_topic_and_session_gets_its_own_file(tmp_path: Path) -> None:
    recorder, _ = make_recorder(tmp_path)
    recorder.handle_payload("v1/laps", json.dumps({"session_key": 1}))
    recorder.handle_payload("v1/location", json.dumps({"session_key": 1}))
    recorder.handle_payload("v1/laps", json.dumps({"session_key": 2}))

    assert (tmp_path / "1" / "v1_laps.jsonl").exists()
    assert (tmp_path / "1" / "v1_location.jsonl").exists()
    assert (tmp_path / "2" / "v1_laps.jsonl").exists()
    assert recorder.stats.session_keys == {"1", "2"}


def test_payload_without_session_key_still_recorded(tmp_path: Path) -> None:
    recorder, _ = make_recorder(tmp_path)
    recorder.handle_payload("v1/meetings", json.dumps({"meeting_key": 1250}))
    assert (tmp_path / UNKNOWN_SESSION / "v1_meetings.jsonl").exists()


def test_data_is_flushed_immediately(tmp_path: Path) -> None:
    """A reader must see the line while the recorder is still running."""
    recorder, _ = make_recorder(tmp_path)
    recorder.handle_payload("v1/laps", json.dumps({"session_key": 3}))
    # No close(), no stop() - read it right now.
    assert (tmp_path / "3" / "v1_laps.jsonl").read_text().strip() != ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("v1/laps", "v1_laps"),
        ("v1/car_data", "v1_car_data"),
        ("../../etc/passwd", "etc_passwd"),
        ("a b\tc", "a_b_c"),
        ("", "FALLBACK"),
        ("   ", "FALLBACK"),
        ("...", "FALLBACK"),
        (None, "FALLBACK"),
        (9999, "9999"),
    ],
)
def test_path_components_are_sanitised(value: object, expected: str) -> None:
    assert safe_component(value, "FALLBACK") == expected


def test_hostile_topic_cannot_escape_the_recordings_directory(tmp_path: Path) -> None:
    recorder, _ = make_recorder(tmp_path)
    recorder.handle_payload("../../../evil", json.dumps({"session_key": "../../oops"}))

    written = [p for p in tmp_path.rglob("*.jsonl")]
    assert len(written) == 1
    assert tmp_path in written[0].parents, "must stay inside recordings_dir"


# -- robustness -------------------------------------------------------------


def test_undecodable_payload_is_still_recorded(tmp_path: Path) -> None:
    recorder, _ = make_recorder(tmp_path)
    recorder.handle_payload("v1/laps", b"this is not json{{{")

    path = tmp_path / UNKNOWN_SESSION / "v1_laps.jsonl"
    payload = json.loads(path.read_text())["payload"]
    assert payload["_raw"] == "this is not json{{{"
    assert "_decode_error" in payload
    assert recorder.stats.undecodable_messages == 1
    assert recorder.stats.messages_recorded == 1


def test_invalid_utf8_is_replaced_not_dropped(tmp_path: Path) -> None:
    recorder, _ = make_recorder(tmp_path)
    recorder.handle_payload("v1/laps", b"\xff\xfe not utf8")
    assert recorder.stats.messages_recorded == 1
    assert (tmp_path / UNKNOWN_SESSION / "v1_laps.jsonl").exists()


def test_subscribers_see_messages_after_they_are_on_disk(tmp_path: Path) -> None:
    recorder, _ = make_recorder(tmp_path)
    seen: list[RawMessage] = []

    def subscriber(message: RawMessage) -> None:
        # The file must already contain the line by the time we are called.
        assert (tmp_path / "5" / "v1_laps.jsonl").exists()
        seen.append(message)

    recorder.add_subscriber(subscriber)
    recorder.handle_payload("v1/laps", json.dumps({"session_key": 5}))

    assert len(seen) == 1
    assert seen[0].topic == "v1/laps"


def test_a_raising_subscriber_cannot_stop_the_recording(tmp_path: Path) -> None:
    """The whole point of the write-first design."""
    recorder, _ = make_recorder(tmp_path)
    good: list[RawMessage] = []

    recorder.add_subscriber(lambda m: (_ for _ in ()).throw(RuntimeError("adapter bug")))
    recorder.add_subscriber(good.append)

    for i in range(3):
        recorder.handle_payload("v1/laps", json.dumps({"session_key": 5, "n": i}))

    lines = (tmp_path / "5" / "v1_laps.jsonl").read_text().strip().split("\n")
    assert len(lines) == 3, "every message recorded despite the broken subscriber"
    assert len(good) == 3, "a later subscriber still runs"
    assert recorder.stats.subscriber_errors == 3


def test_on_message_swallows_errors_so_the_paho_loop_survives(tmp_path: Path) -> None:
    recorder, _ = make_recorder(tmp_path)

    class Exploding:
        topic = "v1/laps"

        @property
        def payload(self) -> bytes:
            raise RuntimeError("cannot read payload")

    recorder._on_message(None, None, Exploding())  # noqa: SLF001
    assert recorder.stats.last_error is not None


def test_writer_is_thread_safe(tmp_path: Path) -> None:
    writer = JsonlWriter(tmp_path)
    errors: list[Exception] = []

    def hammer(worker: int) -> None:
        try:
            for i in range(50):
                writer.write("1", "v1_laps", json.dumps({"worker": worker, "i": i}))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(w,)) for w in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    writer.close()

    assert not errors
    lines = (tmp_path / "1" / "v1_laps.jsonl").read_text().strip().split("\n")
    assert len(lines) == 400
    for line in lines:
        json.loads(line)  # every line must be complete and parseable


# -- connection supervision -------------------------------------------------


def test_connects_authenticates_and_subscribes_to_everything(tmp_path: Path) -> None:
    client = FakeClient()
    recorder, tokens = make_recorder(tmp_path, client_factory=lambda: client)

    recorder.start()
    for _ in range(200):
        if client.subscriptions:
            break
        threading.Event().wait(0.01)
    recorder.stop(timeout=5)

    assert client.connected_to == ("mqtt.openf1.org", 8883)
    assert client.username == "test-user@example.invalid"
    assert client.password == "test-token-placeholder-1", "the MQTT password is the access token"
    # QoS 1 by default (settings.mqtt_qos): the broker retransmits across a
    # brief drop, and the adapter is idempotent so a redelivery is harmless.
    assert client.subscriptions == [("#", 1)]
    assert recorder.stats.connect_count == 1


def test_messages_arriving_via_the_paho_callback_are_recorded(tmp_path: Path) -> None:
    client = FakeClient()
    recorder, _ = make_recorder(tmp_path, client_factory=lambda: client)
    recorder.start()
    for _ in range(200):
        if client.on_message:
            break
        threading.Event().wait(0.01)

    class Message:
        topic = "v1/intervals"
        payload = json.dumps({"session_key": 42, "gap_to_leader": "+1.234"}).encode()

    client.on_message(client, None, Message())
    recorder.stop(timeout=5)

    path = tmp_path / "42" / "v1_intervals.jsonl"
    assert json.loads(path.read_text())["payload"]["gap_to_leader"] == "+1.234"


def test_refused_connection_forces_a_fresh_token(tmp_path: Path) -> None:
    """'Not authorized' almost always means the token went stale.

    The callback itself must not fetch: it runs on paho's network thread and a
    blocking retry loop there stalls the connection. It flags the supervisor,
    whose next cycle forces a refresh instead of reusing the cached token.
    """
    client = FakeClient()
    recorder, tokens = make_recorder(tmp_path, client_factory=lambda: client)

    recorder._client = client  # noqa: SLF001
    recorder._on_connect(client, None, {}, 5, None)  # noqa: SLF001 - rc 5 = not authorized

    assert tokens.calls == [], "no token HTTP inside the MQTT callback"
    assert recorder.needs_token_refresh is True
    assert recorder.stats.connected is False
    assert "refused" in (recorder.stats.last_error or "")

    recorder.start()
    for _ in range(200):
        if tokens.calls:
            break
        threading.Event().wait(0.01)
    recorder.stop(timeout=5)
    assert tokens.calls[0] is True, "the supervisor must force_refresh, not reuse the cached token"


def test_disconnect_is_counted_and_does_not_raise(tmp_path: Path) -> None:
    recorder, _ = make_recorder(tmp_path)
    recorder._on_disconnect(None, None, None, 7, None)  # noqa: SLF001
    assert recorder.stats.disconnect_count == 1
    assert recorder.stats.connected is False


def test_stop_is_idempotent_and_closes_files(tmp_path: Path) -> None:
    recorder, _ = make_recorder(tmp_path)
    recorder.handle_payload("v1/laps", json.dumps({"session_key": 1}))
    recorder.stop(timeout=2)
    recorder.stop(timeout=2)
    assert recorder._writer.open_files == 0  # noqa: SLF001


def test_health_reports_what_friday_needs(tmp_path: Path) -> None:
    recorder, _ = make_recorder(tmp_path)
    recorder.handle_payload("v1/laps", json.dumps({"session_key": 42}))
    recorder.handle_payload("v1/location", json.dumps({"session_key": 42}))
    recorder.handle_payload("v1/laps", json.dumps({"session_key": 42}))

    health = recorder.health()
    assert health["messages_recorded"] == 3
    assert health["topics"] == {"v1/laps": 2, "v1/location": 1}
    assert health["session_keys"] == ["42"]
    assert health["last_message_at"] is not None
    assert "token_expires_at" in health


def test_health_and_logs_never_expose_the_recordings_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """/health is public and logs get pasted into bug reports: neither may
    carry the absolute location of the recordings directory."""
    recorder, _ = make_recorder(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="app.recorder"):
        recorder.handle_payload("v1/laps", json.dumps({"session_key": 42}))
        recorder.handle_payload("v1/location", json.dumps({"session_key": 42}))
    health = recorder.health()

    assert "recordings_dir" not in health
    assert health["recordings_configured"] is True
    assert str(tmp_path) not in json.dumps(health)

    messages = [record.getMessage() for record in caplog.records]
    assert any("recording to" in message for message in messages), "the write must still be logged"
    for message in messages:
        assert str(tmp_path) not in message
    # The write itself still happened where it was told to.
    assert (tmp_path / "42" / "v1_laps.jsonl").exists() or any(tmp_path.rglob("*.jsonl"))


def test_starting_twice_is_an_error(tmp_path: Path) -> None:
    client = FakeClient()
    recorder, _ = make_recorder(tmp_path, client_factory=lambda: client)
    recorder.start()
    with pytest.raises(RuntimeError, match="already started"):
        recorder.start()
    recorder.stop(timeout=5)


# -- reconnection: the behaviour Friday actually depends on -----------------


def test_token_rotation_reconnects_with_a_fresh_token(tmp_path: Path) -> None:
    """The MQTT password is the token, so expiry must force a clean reconnect.

    Waiting to be kicked by the broker could mean losing messages mid-race, so
    the supervisor rotates while the current token is still valid.
    """
    import time

    clients: list[FakeClient] = []
    enough = threading.Event()

    def factory() -> FakeClient:
        client = FakeClient()
        clients.append(client)
        if len(clients) >= 3:
            enough.set()
            time.sleep(0.05)  # throttle so we do not spin thousands of times
        return client

    tokens = FakeTokenProvider()
    tokens.seconds_until_refresh = lambda: 0.0  # type: ignore[method-assign]

    recorder = RawRecorder(
        make_settings(tmp_path),
        tokens,  # type: ignore[arg-type]
        client_factory=factory,
        retry_delay_seconds=0.01,
    )
    recorder.start()
    assert enough.wait(5), "expected repeated reconnects as the token expires"
    recorder.stop(timeout=5)

    first_three = clients[:3]
    assert [c.password for c in first_three] == ["test-token-placeholder-1", "test-token-placeholder-2", "test-token-placeholder-3"]
    assert all(c.subscriptions == [("#", 1)] for c in first_three)
    assert all(c.disconnected for c in first_three), "old connections are torn down"


def test_connect_failure_is_retried_rather_than_fatal(tmp_path: Path) -> None:
    attempts: list[int] = []
    enough = threading.Event()

    class FailingClient(FakeClient):
        def connect(self, host: str, port: int, keepalive: int = 60) -> None:
            attempts.append(1)
            if len(attempts) >= 3:
                enough.set()
            raise OSError("network is unreachable")

    recorder, _ = make_recorder(
        tmp_path, client_factory=FailingClient, retry_delay_seconds=0.01
    )
    recorder.start()
    assert enough.wait(5), "a failed connect must be retried, not give up"
    recorder.stop(timeout=5)

    assert recorder.stats.connected is False
    assert "connect failed" in (recorder.stats.last_error or "")
    assert "OSError" in (recorder.stats.last_error or "")


def test_recording_continues_across_a_reconnect(tmp_path: Path) -> None:
    """Messages from before and after a reconnect land in the same file."""
    clients: list[FakeClient] = []
    recorder, _ = make_recorder(
        tmp_path,
        client_factory=lambda: clients.append(FakeClient()) or clients[-1],  # type: ignore[func-returns-value]
        retry_delay_seconds=0.01,
    )
    recorder.start()
    for _ in range(200):
        if clients and clients[0].on_message:
            break
        threading.Event().wait(0.01)

    class Message:
        def __init__(self, lap: int) -> None:
            self.topic = "v1/laps"
            self.payload = json.dumps({"session_key": 100, "lap_number": lap}).encode()

    clients[0].on_message(clients[0], None, Message(1))
    # Simulate the broker dropping us, then a fresh connection delivering more.
    recorder._on_disconnect(clients[0], None, None, 7, None)  # noqa: SLF001
    clients[0].on_message(clients[0], None, Message(2))
    recorder.stop(timeout=5)

    lines = (tmp_path / "100" / "v1_laps.jsonl").read_text().strip().split("\n")
    assert [json.loads(line)["payload"]["lap_number"] for line in lines] == [1, 2]
    assert recorder.stats.disconnect_count == 1


def test_stop_returns_promptly_even_while_waiting_to_retry(tmp_path: Path) -> None:
    """A long retry pause must not delay shutdown by its full length."""
    import time

    class FailingClient(FakeClient):
        def connect(self, host: str, port: int, keepalive: int = 60) -> None:
            raise OSError("down")

    recorder, _ = make_recorder(
        tmp_path, client_factory=FailingClient, retry_delay_seconds=30.0
    )
    recorder.start()
    threading.Event().wait(0.2)

    started = time.monotonic()
    recorder.stop(timeout=5)
    assert time.monotonic() - started < 2.0, "stop must interrupt the retry wait"


# -- RECORDING_ENABLED=false --------------------------------------------------


def test_recording_disabled_writes_no_file_but_still_counts_and_fans_out(tmp_path: Path) -> None:
    settings = make_settings(tmp_path).model_copy(update={"recording_enabled": False})
    tokens = FakeTokenProvider()
    recorder = RawRecorder(settings, tokens)  # type: ignore[arg-type]

    seen: list[RawMessage] = []
    recorder.add_subscriber(seen.append)
    recorder.start_fan_out()

    recorder.handle_payload("v1/laps", json.dumps({"session_key": 42, "driver_number": 1}))
    recorder.drain(timeout=2)
    recorder.stop(timeout=2)

    assert list(tmp_path.iterdir()) == [], "RECORDING_ENABLED=false must write nothing"
    assert recorder.stats.messages_recorded == 1, "the message was still handled"
    assert recorder.stats.recording_ok is True, "nothing failed - recording was never attempted"
    assert len(seen) == 1, "the live adapter still gets fed"


def test_recording_disabled_logs_once_at_startup(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    settings = make_settings(tmp_path).model_copy(update={"recording_enabled": False})
    tokens = FakeTokenProvider()
    recorder = RawRecorder(settings, tokens, client_factory=lambda: FakeClient())  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING):
        recorder.start()
        recorder.stop(timeout=2)

    matches = [r for r in caplog.records if "recording disabled by RECORDING_ENABLED=false" in r.message]
    assert len(matches) == 1


def test_recording_disabled_health_reports_zero_open_files_and_no_disk_check(tmp_path: Path) -> None:
    settings = make_settings(tmp_path).model_copy(update={"recording_enabled": False})
    recorder = RawRecorder(settings, FakeTokenProvider())  # type: ignore[arg-type]
    recorder.handle_payload("v1/laps", json.dumps({"session_key": 1}))

    health = recorder.health()
    assert health["open_files"] == 0
    assert health["disk_free_bytes"] is None
    assert health["disk_low"] is False
