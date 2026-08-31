"""Raw capture of the OpenF1 live stream to disk.

This is the most important module in the project. A complete recording of a
session weekend is the artefact every later phase is built from, so the rules
here are deliberately strict:

* **No dependency on the UI or on any parsing code.** This module imports
  ``config`` and ``auth`` and nothing else of ours. A bug in the leaderboard,
  the live adapter, or the WebSocket layer cannot stop the recording.
* **Write first, fan out second.** Every message is appended to disk *before*
  any in-process subscriber sees it, and every subscriber callback is wrapped in
  its own ``try``, so a subscriber that raises cannot cost us a message. This is
  the "fan out in-process, do not open two connections" requirement: Phase 2's
  live adapter registers here rather than opening its own MQTT connection.
* **Store bytes, not interpretations.** The payload is written through as-is.
  A message that is not valid JSON is still recorded, wrapped so the replay
  reader can spot it. Nothing in this file knows what a lap or a driver is, and
  in particular nothing here interprets the legacy ``drs`` field.

Connection lifecycle
--------------------
The MQTT password *is* the OAuth2 access token, and it expires after an hour.
An MQTT session that outlives its token gets dropped by the broker at a moment
of the broker's choosing - which could be mid-race. So a supervisor thread
reconnects deliberately a few minutes before expiry, while the current token is
still valid, instead of waiting to be kicked. Unexpected drops are handled by
paho's own automatic reconnect, and an auth failure forces a fresh token.

Run standalone (the Tier A fallback - recording without the UI):
    .venv/bin/python -m app.recorder
"""

from __future__ import annotations

import json
import logging
import re
import signal
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

import paho.mqtt.client as mqtt

from app.auth import AuthError, TokenProvider
from app.config import Settings, get_settings

log = logging.getLogger("app.recorder")  # pinned: __name__ is "__main__" when run standalone

# Subscribe to everything. The docs say topics mirror the REST paths, but we do
# not want to discover on Friday that a topic we hardcoded was renamed.
SUBSCRIBE_TOPIC = "#"
SUBSCRIBE_QOS = 0

MQTT_KEEPALIVE_SECONDS = 60
RECONNECT_MIN_DELAY = 1
RECONNECT_MAX_DELAY = 30

# How often the supervisor wakes to check whether it should rotate the token.
SUPERVISOR_TICK_SECONDS = 5.0

# Filenames must survive whatever the broker sends as a topic or session key.
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
UNKNOWN_SESSION = "_no_session_key"

MessageSubscriber = Callable[["RawMessage"], None]


@dataclass(frozen=True)
class RawMessage:
    """One message as received, before anything has interpreted it."""

    received_at: datetime
    topic: str
    payload: Any
    """Decoded JSON when possible; otherwise ``{"_raw": "<text>", ...}``."""

    def to_line(self) -> str:
        """The exact on-disk representation: one JSON object, one line."""
        return json.dumps(
            {
                "received_at": self.received_at.isoformat(timespec="milliseconds"),
                "topic": self.topic,
                "payload": self.payload,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )


@dataclass
class RecorderStats:
    """Everything /health needs to answer 'is the feed alive?' on Friday."""

    connected: bool = False
    connect_count: int = 0
    disconnect_count: int = 0
    messages_recorded: int = 0
    undecodable_messages: int = 0
    subscriber_errors: int = 0
    last_message_at: datetime | None = None
    last_error: str | None = None
    topic_counts: Counter[str] = field(default_factory=Counter)
    session_keys: set[str] = field(default_factory=set)

    def snapshot(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "connect_count": self.connect_count,
            "disconnect_count": self.disconnect_count,
            "messages_recorded": self.messages_recorded,
            "undecodable_messages": self.undecodable_messages,
            "subscriber_errors": self.subscriber_errors,
            "last_message_at": (
                self.last_message_at.isoformat(timespec="milliseconds")
                if self.last_message_at
                else None
            ),
            "last_error": self.last_error,
            "session_keys": sorted(self.session_keys),
            "topics": dict(sorted(self.topic_counts.items())),
        }


def safe_component(value: object, fallback: str) -> str:
    """Turn a topic or session key into something safe to use as a path part.

    The fallback is returned verbatim, never sanitised: it is our own constant,
    and stripping it would silently rename ``_no_session_key`` to
    ``no_session_key``. ``None`` is treated as absent rather than stringified,
    or a payload with a null ``session_key`` would land in a folder called
    literally "None".
    """
    if value is None:
        return fallback
    text = str(value).strip()
    if not text:
        return fallback
    cleaned = _SAFE_CHARS.sub("_", text).strip("._-")
    return cleaned or fallback


class JsonlWriter:
    """Append-only ``.jsonl`` files, one per session_key and topic.

    Files are opened lazily and kept open: during a session the same dozen
    files are written to hundreds of times a second, and reopening per message
    would be pure overhead.

    ``flush()`` on every line hands the bytes to the OS, so a crash of *this*
    process loses nothing. Deliberately not ``os.fsync``: that waits on the
    physical disk and at a few hundred messages a second it would stall the
    MQTT network thread. Losing the last few milliseconds to a kernel panic is
    an acceptable trade for not dropping messages during a race.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._handles: dict[tuple[str, str], TextIO] = {}
        self._lock = threading.Lock()

    def write(self, session_key: str, topic: str, line: str) -> Path:
        key = (session_key, topic)
        with self._lock:
            handle = self._handles.get(key)
            if handle is None:
                directory = self._root / session_key
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / f"{topic}.jsonl"
                handle = path.open("a", encoding="utf-8")
                self._handles[key] = handle
                log.info("recording to %s", path)
            handle.write(line)
            handle.write("\n")
            handle.flush()
            return Path(handle.name)

    def close(self) -> None:
        with self._lock:
            for handle in self._handles.values():
                try:
                    handle.flush()
                    handle.close()
                except Exception:  # noqa: BLE001 - closing must never raise
                    log.exception("failed closing a recording file")
            self._handles.clear()

    @property
    def open_files(self) -> int:
        return len(self._handles)


class RawRecorder:
    """Subscribes to the live stream and appends every message to disk."""

    def __init__(
        self,
        settings: Settings | None = None,
        token_provider: TokenProvider | None = None,
        *,
        client_factory: Callable[[], mqtt.Client] | None = None,
        retry_delay_seconds: float = RECONNECT_MAX_DELAY,
    ) -> None:
        self._settings = settings or get_settings()
        self._retry_delay = retry_delay_seconds
        self._tokens = token_provider or TokenProvider(self._settings)
        self._client_factory = client_factory or self._default_client
        self._writer = JsonlWriter(self._settings.recordings_dir)

        self.stats = RecorderStats()
        self._subscribers: list[MessageSubscriber] = []
        self._subscriber_lock = threading.Lock()

        self._client: mqtt.Client | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._rotate_now = threading.Event()

    # -- fan-out ------------------------------------------------------------

    def add_subscriber(self, callback: MessageSubscriber) -> None:
        """Register an in-process consumer of the same stream.

        Phase 2's live adapter uses this instead of opening a second MQTT
        connection. Subscribers are called after the message is safely on disk,
        and their exceptions are swallowed and counted.
        """
        with self._subscriber_lock:
            self._subscribers.append(callback)

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("recorder already started")
        self._settings.require_credentials()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._supervise, name="openf1-recorder", daemon=True
        )
        self._thread.start()
        log.info("recorder thread started")

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._rotate_now.set()
        client = self._client
        if client is not None:
            try:
                client.disconnect()
                client.loop_stop()
            except Exception:  # noqa: BLE001
                log.exception("error stopping the MQTT client")
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._writer.close()
        self._tokens.close()
        log.info(
            "recorder stopped after %d message(s) across %d topic(s)",
            self.stats.messages_recorded,
            len(self.stats.topic_counts),
        )

    # -- supervisor ---------------------------------------------------------

    def _default_client(self) -> mqtt.Client:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
        client.tls_set()  # system CA bundle; the broker speaks TLS on 8883
        client.reconnect_delay_set(RECONNECT_MIN_DELAY, RECONNECT_MAX_DELAY)
        return client

    def _supervise(self) -> None:
        """Own the connection for the life of the recorder.

        One iteration == one token's worth of connection. We come back around
        when the token is close to expiring, or when something went wrong.
        """
        while not self._stop.is_set():
            try:
                token = self._tokens.get_token()
            except AuthError as exc:
                self.stats.last_error = str(exc)
                log.error("cannot authenticate: %s", exc)
                # get_token has already backed off internally; pause before the
                # next full cycle so a hard failure does not spin.
                self._stop.wait(self._retry_delay)
                continue

            self._rotate_now.clear()
            client = self._client_factory()
            self._client = client
            client.username_pw_set(self._tokens.username, token)
            client.on_connect = self._on_connect
            client.on_message = self._on_message
            client.on_disconnect = self._on_disconnect
            client.on_subscribe = self._on_subscribe

            try:
                log.info(
                    "connecting to %s:%d (TLS)",
                    self._settings.mqtt_host,
                    self._settings.mqtt_port,
                )
                client.connect(
                    self._settings.mqtt_host,
                    self._settings.mqtt_port,
                    keepalive=MQTT_KEEPALIVE_SECONDS,
                )
                client.loop_start()
            except Exception as exc:  # noqa: BLE001 - any failure means retry
                self.stats.last_error = f"connect failed: {type(exc).__name__}: {exc}"
                log.error("MQTT connect failed: %s", self.stats.last_error)
                self._safe_teardown(client)
                self._stop.wait(self._retry_delay)
                continue

            self._hold_connection()
            self._safe_teardown(client)

        self._writer.close()

    def _hold_connection(self) -> None:
        """Stay connected until the token needs rotating, or we are stopping."""
        while not self._stop.is_set() and not self._rotate_now.is_set():
            remaining = self._tokens.seconds_until_refresh()
            if remaining <= 0:
                log.info("token nearing expiry - reconnecting with a fresh one")
                return
            self._stop.wait(min(SUPERVISOR_TICK_SECONDS, remaining))

    def _safe_teardown(self, client: mqtt.Client) -> None:
        try:
            client.loop_stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self.stats.connected = False
        if self._client is client:
            self._client = None

    # -- MQTT callbacks -----------------------------------------------------

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        connect_flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        if getattr(reason_code, "is_failure", False) or reason_code != 0:
            self.stats.last_error = f"connect refused: {reason_code}"
            log.error("MQTT connection refused: %s", reason_code)
            # 'not authorized' almost always means a stale token.
            self._tokens.get_token(force_refresh=True)
            self._rotate_now.set()
            return

        self.stats.connected = True
        self.stats.connect_count += 1
        client.subscribe(SUBSCRIBE_TOPIC, qos=SUBSCRIBE_QOS)
        log.info(
            "connected, subscribed to %r - waiting for messages "
            "(quiet outside a session window is expected)",
            SUBSCRIBE_TOPIC,
        )

    def _on_subscribe(
        self,
        client: mqtt.Client,
        userdata: Any,
        mid: int,
        reason_code_list: Any,
        properties: Any = None,
    ) -> None:
        log.info("subscription acknowledged: %s", reason_code_list)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        disconnect_flags: Any = None,
        reason_code: Any = None,
        properties: Any = None,
    ) -> None:
        self.stats.connected = False
        self.stats.disconnect_count += 1
        if self._stop.is_set():
            return
        log.warning("MQTT disconnected (%s) - paho will reconnect", reason_code)

    def _on_message(self, client: mqtt.Client, userdata: Any, message: Any) -> None:
        """Hot path. Must never raise: an exception here kills the paho loop."""
        try:
            self.handle_payload(message.topic, message.payload)
        except Exception as exc:  # noqa: BLE001
            self.stats.last_error = f"message handling failed: {type(exc).__name__}"
            log.exception("failed to record a message on %r", getattr(message, "topic", "?"))

    # -- recording ----------------------------------------------------------

    def handle_payload(self, topic: str, payload: bytes | str) -> RawMessage:
        """Decode, write, then fan out. Public so tests can inject messages."""
        received_at = datetime.now(timezone.utc)

        if isinstance(payload, bytes):
            text = payload.decode("utf-8", errors="replace")
        else:
            text = payload

        try:
            decoded: Any = json.loads(text)
        except ValueError as exc:
            # Still recorded. Whatever it was, we want it in the artefact.
            decoded = {"_raw": text, "_decode_error": str(exc)}
            self.stats.undecodable_messages += 1

        raw = RawMessage(received_at=received_at, topic=topic, payload=decoded)

        session_key = UNKNOWN_SESSION
        if isinstance(decoded, dict):
            session_key = safe_component(decoded.get("session_key"), UNKNOWN_SESSION)

        # Disk first, always.
        self._writer.write(session_key, safe_component(topic, "unknown_topic"), raw.to_line())

        self.stats.messages_recorded += 1
        self.stats.last_message_at = received_at
        self.stats.topic_counts[topic] += 1
        self.stats.session_keys.add(session_key)

        self._fan_out(raw)
        return raw

    def _fan_out(self, message: RawMessage) -> None:
        with self._subscriber_lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber(message)
            except Exception:  # noqa: BLE001 - a subscriber must never cost a message
                self.stats.subscriber_errors += 1
                log.exception("subscriber raised; recording is unaffected")

    # -- introspection ------------------------------------------------------

    def health(self) -> dict[str, Any]:
        data = self.stats.snapshot()
        expiry = self._tokens.current_expiry
        data["token_expires_at"] = expiry.isoformat(timespec="seconds") if expiry else None
        data["open_files"] = self._writer.open_files
        data["subscribers"] = len(self._subscribers)
        return data


def main() -> None:  # pragma: no cover - exercised by hand, not by pytest
    """Standalone recorder: capture a session with no UI in the way."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings = get_settings()
    if not settings.live_mode:
        log.warning("LIVE_MODE is false in backend/.env - set it to true to record")

    recorder = RawRecorder(settings)
    finished = threading.Event()

    def shutdown(signum: int, _frame: Any) -> None:
        log.info("signal %s received, shutting down", signum)
        finished.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    recorder.start()
    try:
        while not finished.is_set():
            finished.wait(30)
            if not finished.is_set():
                stats = recorder.stats
                log.info(
                    "status: connected=%s messages=%d topics=%d last=%s",
                    stats.connected,
                    stats.messages_recorded,
                    len(stats.topic_counts),
                    stats.last_message_at.isoformat(timespec="seconds")
                    if stats.last_message_at
                    else "never",
                )
    finally:
        recorder.stop()


if __name__ == "__main__":  # pragma: no cover
    main()
