"""Raw capture of the OpenF1 live stream to disk.

This is the most important module in the project. A complete recording of a
session weekend is the artefact every later phase is built from, so the rules
here are deliberately strict:

* **No dependency on the UI or on any parsing code.** This module imports
  ``config`` and ``auth`` and nothing else of ours. A bug in the leaderboard,
  the live adapter, or the WebSocket layer cannot stop the recording.
* **Write first, fan out second.** Every message is appended to disk *before*
  any in-process subscriber sees it. Subscribers run on their own thread, fed
  through a bounded queue: a slow adapter can lose *queue* messages (counted,
  visible in /health) but can never slow the MQTT network thread or cost the
  recording a message.
* **Store bytes, not interpretations.** The payload is written through as-is.
  A message that is not valid JSON is still recorded, wrapped so the replay
  reader can spot it. Nothing in this file knows what a lap or a driver is, and
  in particular nothing here interprets the legacy ``drs`` field.
* **Never claim more than we know.** A write failure, a disconnect after data
  had started flowing, or a gap in OpenF1's ``_id`` sequence all mark the
  recording as possibly incomplete. That flag never resets.

Connection lifecycle
--------------------
The MQTT password *is* the OAuth2 access token, and it expires after an hour.
An MQTT session that outlives its token gets dropped by the broker at a moment
of the broker's choosing - which could be mid-race. So a supervisor thread
reconnects deliberately a few minutes before expiry, while the current token is
still valid, instead of waiting to be kicked. Unexpected drops are handled by
paho's own automatic reconnect. A refused connection *signals* the supervisor,
which fetches a fresh token on its own thread: the paho callback never does
HTTP, because a blocking retry loop there stalls the network loop.

Run standalone (the Tier A fallback - recording without the UI):
    .venv/bin/python -m app.recorder
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import queue
import re
import shutil
import signal
import sys
import threading
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, TextIO

import paho.mqtt.client as mqtt

from app.auth import AuthError, TokenProvider
from app.config import MissingCredentials, Settings, get_settings

log = logging.getLogger("app.recorder")  # pinned: __name__ is "__main__" when run standalone

# Subscribe to everything. The docs say topics mirror the REST paths, but we do
# not want to discover on Friday that a topic we hardcoded was renamed.
SUBSCRIBE_TOPIC = "#"

MQTT_KEEPALIVE_SECONDS = 60
RECONNECT_MIN_DELAY = 1
RECONNECT_MAX_DELAY = 30

# How often the supervisor wakes to check whether it should rotate the token.
SUPERVISOR_TICK_SECONDS = 5.0

# MQTT CONNACK codes meaning "the credentials were refused": v3.1.1 4 (bad
# user name or password) and 5 (not authorized); v5 0x86 and 0x87.
AUTH_REFUSED_CODES = frozenset({4, 5, 134, 135})

# Log the first write failure of a burst, then only every Nth: at a few
# hundred messages a second a full disk must not also fill the log.
WRITE_FAILURE_LOG_EVERY = 1000

# Filenames must survive whatever the broker sends as a topic or session key.
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
UNKNOWN_SESSION = "_no_session_key"

MessageSubscriber = Callable[["RawMessage"], None]

RecordingErrorKind = Literal["disk_full", "permission", "io"]
RecordingStage = Literal["mkdir", "open", "write", "flush"]


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


class RecordingError(Exception):
    """A message could not be put on disk. Classified so /health can say why."""

    def __init__(self, kind: RecordingErrorKind, stage: RecordingStage, message: str) -> None:
        super().__init__(f"{kind} during {stage}: {message}")
        self.kind: RecordingErrorKind = kind
        self.stage: RecordingStage = stage


def classify_os_error(exc: OSError, stage: RecordingStage) -> RecordingError:
    code = exc.errno
    name = errno.errorcode.get(code, str(code)) if code is not None else type(exc).__name__
    detail = f"{exc.strerror or type(exc).__name__} ({name})"
    if code in (errno.ENOSPC, errno.EDQUOT):
        return RecordingError("disk_full", stage, detail)
    if code in (errno.EACCES, errno.EPERM, errno.EROFS) or isinstance(exc, PermissionError):
        return RecordingError("permission", stage, detail)
    return RecordingError("io", stage, detail)


class IdSequenceTracker:
    """Watches OpenF1's ``_id`` ordinal for gaps, duplicates and reordering.

    Exact within the last ``recent`` ids for duplicate detection, which covers
    QoS 1 redeliveries; beyond that a repeat would count as out-of-order.
    """

    def __init__(self, recent: int = 512) -> None:
        self.count = 0
        self.duplicates = 0
        self.out_of_order = 0
        self.min_id: int | None = None
        self.max_id: int | None = None
        self.last_id: int | None = None
        self._recent: deque[int] = deque(maxlen=recent)
        self._recent_set: set[int] = set()

    def observe(self, message_id: int) -> None:
        self.count += 1
        if message_id in self._recent_set:
            self.duplicates += 1
            return
        if len(self._recent) == self._recent.maxlen:
            self._recent_set.discard(self._recent[0])
        self._recent.append(message_id)
        self._recent_set.add(message_id)

        if self.last_id is not None and message_id < self.last_id:
            self.out_of_order += 1
        self.last_id = message_id
        self.min_id = message_id if self.min_id is None else min(self.min_id, message_id)
        self.max_id = message_id if self.max_id is None else max(self.max_id, message_id)

    @property
    def distinct(self) -> int:
        return self.count - self.duplicates

    @property
    def expected(self) -> int:
        if self.min_id is None or self.max_id is None:
            return 0
        return self.max_id - self.min_id + 1

    @property
    def missing(self) -> int:
        """Ids inside the observed range that were never seen. Negative is
        impossible for a single sequence; the recorder uses that fact to tell
        a per-topic sequence from a global one."""
        return self.expected - self.distinct

    def snapshot(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "min_id": self.min_id,
            "max_id": self.max_id,
            "duplicates": self.duplicates,
            "out_of_order": self.out_of_order,
            "missing": max(0, self.missing),
        }


@dataclass
class RecorderStats:
    """Everything /health needs to answer 'is the feed alive?' on Friday."""

    connected: bool = False
    connect_count: int = 0
    disconnect_count: int = 0
    disconnects_after_data: int = 0
    connect_failures: int = 0
    messages_recorded: int = 0
    undecodable_messages: int = 0
    subscriber_errors: int = 0
    last_message_at: datetime | None = None
    last_error: str | None = None
    last_error_kind: str | None = None
    topic_counts: Counter[str] = field(default_factory=Counter)
    session_keys: set[str] = field(default_factory=set)

    auth_state: str = "unauthenticated"
    """unauthenticated | authenticating | authenticated | failed"""
    auth_failures: int = 0

    recording_ok: bool = True
    write_failures: int = 0
    last_write_error: str | None = None
    fanout_dropped: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "connect_count": self.connect_count,
            "disconnect_count": self.disconnect_count,
            "disconnects_after_data": self.disconnects_after_data,
            "connect_failures": self.connect_failures,
            "messages_recorded": self.messages_recorded,
            "undecodable_messages": self.undecodable_messages,
            "subscriber_errors": self.subscriber_errors,
            "last_message_at": (
                self.last_message_at.isoformat(timespec="milliseconds")
                if self.last_message_at
                else None
            ),
            "last_error": self.last_error,
            "last_error_kind": self.last_error_kind,
            "session_keys": sorted(self.session_keys),
            "topics": dict(sorted(self.topic_counts.items())),
            "auth_state": self.auth_state,
            "auth_failures": self.auth_failures,
            "recording_ok": self.recording_ok,
            "write_failures": self.write_failures,
            "recording_error": self.last_write_error,
            "fanout_dropped": self.fanout_dropped,
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

    Any OS failure - mkdir, open, write, flush - is raised as a classified
    ``RecordingError`` and the handle is discarded, so the next write retries
    from scratch rather than writing into a broken file object.
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
                try:
                    directory.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise classify_os_error(exc, "mkdir") from exc
                path = directory / f"{topic}.jsonl"
                try:
                    handle = path.open("a", encoding="utf-8")
                except OSError as exc:
                    raise classify_os_error(exc, "open") from exc
                self._handles[key] = handle
                # Relative to the recordings root on purpose: the absolute
                # location of the capture is not something the logs need.
                log.info("recording to %s/%s.jsonl", session_key, topic)
            try:
                handle.write(line)
                handle.write("\n")
            except OSError as exc:
                self._discard(key)
                raise classify_os_error(exc, "write") from exc
            try:
                handle.flush()
            except OSError as exc:
                self._discard(key)
                raise classify_os_error(exc, "flush") from exc
            return Path(handle.name)

    def _discard(self, key: tuple[str, str]) -> None:
        handle = self._handles.pop(key, None)
        if handle is not None:
            try:
                handle.close()
            except Exception:  # noqa: BLE001 - already failing; nothing more to do
                pass

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

    def disk_free_bytes(self) -> int | None:
        """Free space on the volume holding the recordings, or None if unknown."""
        probe = self._root
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        try:
            return shutil.disk_usage(probe).free
        except OSError:
            return None


class FanOutQueue:
    """Bounded hand-off from the MQTT network thread to in-process subscribers.

    ``submit`` never blocks: if the queue is full the message is dropped from
    the *fan-out only* (it is already on disk) and counted. When the worker is
    not running - replay, tests - delivery is inline so behaviour is simple.
    """

    def __init__(
        self,
        maxsize: int,
        *,
        on_error: Callable[[Exception], None] | None = None,
        thread_name: str = "openf1-fanout",
    ) -> None:
        self._queue: queue.Queue[RawMessage | None] = queue.Queue(maxsize=max(1, maxsize))
        self._subscribers: list[MessageSubscriber] = []
        self._lock = threading.Lock()
        self._on_error = on_error
        self._thread_name = thread_name
        self._thread: threading.Thread | None = None
        self._pending = 0
        self._idle = threading.Condition()
        self.dropped = 0
        self.high_water = 0

    @property
    def maxsize(self) -> int:
        return self._queue.maxsize

    @property
    def qsize(self) -> int:
        return self._queue.qsize()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def add_subscriber(self, callback: MessageSubscriber) -> None:
        with self._lock:
            self._subscribers.append(callback)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def start(self) -> None:
        if self.running:
            return
        self._thread = threading.Thread(target=self._run, name=self._thread_name, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        thread = self._thread
        if thread is None:
            return
        self._queue.put(None)  # sentinel; blocks only if full, briefly
        thread.join(timeout=timeout)
        self._thread = None

    def submit(self, message: RawMessage) -> bool:
        if not self.running:
            self._deliver(message)
            return True
        try:
            self._queue.put_nowait(message)
        except queue.Full:
            self.dropped += 1
            return False
        with self._idle:
            self._pending += 1
        size = self._queue.qsize()
        if size > self.high_water:
            self.high_water = size
        return True

    def drain(self, timeout: float = 5.0) -> bool:
        """Block until every submitted message has been delivered."""
        with self._idle:
            return self._idle.wait_for(lambda: self._pending == 0, timeout=timeout)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self._deliver(item)
            finally:
                with self._idle:
                    self._pending -= 1
                    self._idle.notify_all()

    def _deliver(self, message: RawMessage) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber(message)
            except Exception as exc:  # noqa: BLE001 - a subscriber must never cost a message
                if self._on_error is not None:
                    self._on_error(exc)


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
        # None when RECORDING_ENABLED=false: the recorder still connects and
        # feeds the live adapter, it just never touches disk. Logged once in
        # start(), which is the actual startup of a recording attempt.
        self._writer = JsonlWriter(self._settings.recordings_dir) if self._settings.recording_enabled else None

        self.stats = RecorderStats()
        self._fanout = FanOutQueue(self._settings.fanout_queue_size, on_error=self._subscriber_failed)
        self._id_by_topic: dict[str, IdSequenceTracker] = {}
        self._id_global = IdSequenceTracker()
        self._write_failure_burst = 0

        self._client: mqtt.Client | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._rotate_now = threading.Event()
        self._needs_refresh = False

    # -- fan-out ------------------------------------------------------------

    def add_subscriber(self, callback: MessageSubscriber) -> None:
        """Register an in-process consumer of the same stream.

        Phase 2's live adapter uses this instead of opening a second MQTT
        connection. Subscribers are called after the message is safely on disk,
        on the fan-out thread, and their exceptions are swallowed and counted.
        """
        self._fanout.add_subscriber(callback)

    def start_fan_out(self) -> None:
        """Start the subscriber thread without connecting. ``start()`` does this."""
        self._fanout.start()

    def drain(self, timeout: float = 5.0) -> bool:
        """Wait until subscribers have seen everything recorded so far."""
        return self._fanout.drain(timeout=timeout)

    def _subscriber_failed(self, exc: Exception) -> None:
        self.stats.subscriber_errors += 1
        log.error("subscriber raised %s; recording is unaffected", type(exc).__name__, exc_info=exc)

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("recorder already started")
        self._settings.require_credentials()
        if self._writer is None:
            log.warning("recording disabled by RECORDING_ENABLED=false")
        self._stop.clear()
        self._fanout.start()
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
        self._fanout.drain(timeout=min(timeout, 2.0))
        self._fanout.stop(timeout=timeout)
        if self._writer is not None:
            self._writer.close()
        self._tokens.close()
        log.info(
            "recorder stopped after %d message(s) across %d topic(s)%s",
            self.stats.messages_recorded,
            len(self.stats.topic_counts),
            " - recording may be INCOMPLETE" if self.may_be_incomplete else "",
        )

    # -- supervisor ---------------------------------------------------------

    def _default_client(self) -> mqtt.Client:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
        client.tls_set()  # system CA bundle; the broker speaks TLS on 8883
        client.reconnect_delay_set(RECONNECT_MIN_DELAY, RECONNECT_MAX_DELAY)
        return client

    @property
    def needs_token_refresh(self) -> bool:
        return self._needs_refresh

    def _supervise(self) -> None:
        """Own the connection for the life of the recorder.

        One iteration == one token's worth of connection. We come back around
        when the token is close to expiring, or when something went wrong.
        """
        while not self._stop.is_set():
            force = self._needs_refresh
            self._needs_refresh = False
            self.stats.auth_state = "authenticating"
            try:
                token = self._tokens.get_token(force_refresh=force)
            except (AuthError, MissingCredentials) as exc:
                kind = getattr(exc, "kind", "config")
                self.stats.auth_state = "failed"
                self.stats.auth_failures += 1
                self._record_error(f"authentication failed ({kind}): {exc}", "auth")
                log.error("cannot authenticate (%s): %s", kind, exc)
                # get_token has already backed off internally; pause before the
                # next full cycle so a hard failure does not spin.
                self._stop.wait(self._retry_delay)
                continue
            except Exception as exc:  # noqa: BLE001 - never let the supervisor die
                self.stats.auth_state = "failed"
                self.stats.auth_failures += 1
                self._record_error(f"authentication failed (unexpected {type(exc).__name__})", "auth")
                log.exception("unexpected error obtaining a token")
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
                self.stats.connect_failures += 1
                self._record_error(f"connect failed: {type(exc).__name__}: {exc}", "network")
                log.error("MQTT connect failed: %s", self.stats.last_error)
                self._safe_teardown(client)
                self._stop.wait(self._retry_delay)
                continue

            self._hold_connection()
            self._safe_teardown(client)
            if self._needs_refresh and not self._stop.is_set():
                # Refused: brief pause so a permanently bad token does not spin.
                self._stop.wait(min(self._retry_delay, 1.0))

        if self._writer is not None:
            self._writer.close()

    def _hold_connection(self) -> None:
        """Stay connected until the token needs rotating, or we are stopping."""
        while not self._stop.is_set() and not self._rotate_now.is_set():
            remaining = self._tokens.seconds_until_refresh()
            if remaining <= 0:
                log.info("token nearing expiry - reconnecting with a fresh one")
                return
            self._rotate_now.wait(min(SUPERVISOR_TICK_SECONDS, remaining))

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

    def _record_error(self, message: str, kind: str) -> None:
        self.stats.last_error = message
        self.stats.last_error_kind = kind

    # -- MQTT callbacks -----------------------------------------------------

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        connect_flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        code = _reason_code_value(reason_code)
        if getattr(reason_code, "is_failure", False) or code != 0:
            self.stats.connected = False
            if code in AUTH_REFUSED_CODES:
                # A stale or rejected token. Signal the supervisor: it fetches
                # a fresh one on its own thread. No HTTP here - this is paho's
                # network thread and a blocking retry loop would stall it.
                self.stats.auth_state = "failed"
                self.stats.auth_failures += 1
                self._needs_refresh = True
                self._record_error(f"connect refused: not authorized (rc={code})", "auth")
                log.error("MQTT connection refused (rc=%s): token rejected, will refresh", code)
            else:
                self._record_error(f"connect refused: {reason_code}", "network")
                log.error("MQTT connection refused: %s", reason_code)
            self._rotate_now.set()
            return

        self.stats.connected = True
        self.stats.connect_count += 1
        self.stats.auth_state = "authenticated"
        client.subscribe(SUBSCRIBE_TOPIC, qos=self._settings.mqtt_qos)
        log.info(
            "connected, subscribed to %r at QoS %d - waiting for messages "
            "(quiet outside a session window is expected)",
            SUBSCRIBE_TOPIC,
            self._settings.mqtt_qos,
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
        if self.stats.messages_recorded > 0:
            # Anything published while we were away is gone at QoS 0, and only
            # maybe retransmitted at QoS 1. Either way we cannot promise a
            # complete capture any more.
            self.stats.disconnects_after_data += 1
        if self._stop.is_set():
            return
        log.warning("MQTT disconnected (%s) - paho will reconnect", reason_code)

    def _on_message(self, client: mqtt.Client, userdata: Any, message: Any) -> None:
        """Hot path. Must never raise: an exception here kills the paho loop."""
        try:
            self.handle_payload(message.topic, message.payload)
        except Exception as exc:  # noqa: BLE001
            self._record_error(f"message handling failed: {type(exc).__name__}", "internal")
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
        message_id: Any = None
        if isinstance(decoded, dict):
            session_key = safe_component(decoded.get("session_key"), UNKNOWN_SESSION)
            message_id = decoded.get("_id")

        # Disk first, always - unless RECORDING_ENABLED=false, in which case
        # there is no writer at all. messages_recorded still counts (the
        # message really was handled) and recording_ok stays true (nothing
        # is failing; recording was simply never attempted).
        if self._writer is not None:
            try:
                self._writer.write(session_key, safe_component(topic, "unknown_topic"), raw.to_line())
            except RecordingError as exc:
                self._write_failed(exc, topic)
            else:
                self._write_succeeded()
                self.stats.messages_recorded += 1
        else:
            self.stats.messages_recorded += 1

        self.stats.last_message_at = received_at
        self.stats.topic_counts[topic] += 1
        self.stats.session_keys.add(session_key)

        if isinstance(message_id, int) and not isinstance(message_id, bool):
            tracker = self._id_by_topic.get(topic)
            if tracker is None:
                tracker = IdSequenceTracker()
                self._id_by_topic[topic] = tracker
            tracker.observe(message_id)
            self._id_global.observe(message_id)

        # Fan out even if the write failed: the dashboard can still show the
        # data, and the failure is already visible in health.
        if not self._fanout.submit(raw):
            self.stats.fanout_dropped = self._fanout.dropped
            if self._fanout.dropped == 1 or self._fanout.dropped % WRITE_FAILURE_LOG_EVERY == 0:
                log.warning(
                    "fan-out queue full (%d): dropped %d message(s) from the live adapter; "
                    "the recording is unaffected",
                    self._fanout.maxsize,
                    self._fanout.dropped,
                )
        return raw

    def _write_failed(self, exc: RecordingError, topic: str) -> None:
        self.stats.write_failures += 1
        self.stats.recording_ok = False
        self.stats.last_write_error = str(exc)
        self._record_error(f"recording failed: {exc}", exc.kind)
        self._write_failure_burst += 1
        if self._write_failure_burst == 1 or self._write_failure_burst % WRITE_FAILURE_LOG_EVERY == 0:
            log.error(
                "RECORDING FAILED (%s during %s) on %r: %s - %d failure(s) in this burst, "
                "%d total. Messages are NOT being written to disk.",
                exc.kind,
                exc.stage,
                topic,
                exc,
                self._write_failure_burst,
                self.stats.write_failures,
            )

    def _write_succeeded(self) -> None:
        if not self.stats.recording_ok:
            log.warning(
                "recording resumed after %d failed write(s); the capture has a gap",
                self._write_failure_burst,
            )
        self.stats.recording_ok = True
        self._write_failure_burst = 0

    # -- introspection ------------------------------------------------------

    @property
    def messages_possibly_lost(self) -> int:
        """Estimated from ``_id`` gaps. See ``_id_tracking``."""
        return self._id_tracking()["messages_possibly_lost"]

    @property
    def may_be_incomplete(self) -> bool:
        return (
            self.stats.write_failures > 0
            or self.stats.disconnects_after_data > 0
            or self.messages_possibly_lost > 0
        )

    def _id_tracking(self) -> dict[str, Any]:
        """Gap estimate under both plausible models of ``_id``.

        OpenF1 documents ``_id`` as ever-increasing but not whether the
        sequence is per topic or shared across topics. If it is per topic, the
        global range is smaller than the total count (interleaving), so the
        global "missing" goes negative - which identifies the model. If it is
        global, the per-topic ranges overlap and the per-topic estimate is
        wildly high while the global one is exact. Both are reported, and the
        model whose arithmetic is consistent is the one used.
        """
        per_topic = {topic: t.snapshot() for topic, t in sorted(self._id_by_topic.items())}
        per_topic_missing = sum(max(0, t.missing) for t in self._id_by_topic.values())
        global_missing = self._id_global.missing
        if self._id_global.count == 0:
            model = "unknown"
            lost = 0
        elif global_missing < 0:
            model = "per_topic"
            lost = per_topic_missing
        else:
            model = "global"
            lost = min(global_missing, per_topic_missing) if len(self._id_by_topic) > 1 else global_missing
        return {
            "model": model,
            "messages_possibly_lost": lost,
            "per_topic": per_topic,
            "global": self._id_global.snapshot(),
        }

    def health(self) -> dict[str, Any]:
        data = self.stats.snapshot()
        expiry = self._tokens.current_expiry
        data["token_expires_at"] = expiry.isoformat(timespec="seconds") if expiry else None
        data["open_files"] = self._writer.open_files if self._writer is not None else 0
        data["subscribers"] = self._fanout.subscriber_count
        data["fanout_dropped"] = self._fanout.dropped
        data["fanout_queue"] = {
            "size": self._fanout.qsize,
            "maxsize": self._fanout.maxsize,
            "high_water": self._fanout.high_water,
            "dropped": self._fanout.dropped,
            "running": self._fanout.running,
        }
        ids = self._id_tracking()
        data["id_tracking"] = ids
        data["messages_possibly_lost"] = ids["messages_possibly_lost"]
        data["may_be_incomplete"] = self.may_be_incomplete
        # No writer means nothing is tracking the recordings volume; disk
        # pressure there is not this process's problem when it never writes.
        free = self._writer.disk_free_bytes() if self._writer is not None else None
        data["disk_free_bytes"] = free
        data["disk_low"] = free is not None and free < self._settings.recorder_min_free_bytes
        # A flag, never the path: /health is public and the absolute
        # location of the recordings is nobody else's business.
        data["recordings_configured"] = True
        return data


def _reason_code_value(reason_code: Any) -> int:
    value = getattr(reason_code, "value", reason_code)
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


# -- standalone ---------------------------------------------------------------


def run_standalone(
    settings: Settings,
    *,
    force: bool = False,
    recorder_factory: Callable[[Settings], RawRecorder] = RawRecorder,
    stop_event: threading.Event | None = None,
    status_interval: float = 30.0,
) -> int:
    """Run the recorder until ``stop_event`` is set. Returns a process exit code.

    Honours ``LIVE_MODE=false``: nothing is built, no token is requested and no
    connection is opened unless ``force`` is given explicitly. The suite runs
    against the real ``backend/.env`` on this machine, so this check is what
    keeps an accidental ``make record`` from burning supporter quota.
    """
    if not settings.live_mode and not force:
        log.error(
            "LIVE_MODE is false in backend/.env - refusing to record. Set LIVE_MODE=true, "
            "or pass --force-live (or RECORDER_FORCE_LIVE=1) to override deliberately."
        )
        return 2
    if force and not settings.live_mode:
        log.warning("LIVE_MODE is false but --force-live was given: recording anyway")

    recorder = recorder_factory(settings)
    finished = stop_event if stop_event is not None else threading.Event()

    recorder.start()
    try:
        while not finished.is_set():
            finished.wait(status_interval)
            if not finished.is_set():
                stats = recorder.stats
                log.info(
                    "status: connected=%s auth=%s messages=%d topics=%d last=%s recording_ok=%s",
                    stats.connected,
                    stats.auth_state,
                    stats.messages_recorded,
                    len(stats.topic_counts),
                    stats.last_message_at.isoformat(timespec="seconds")
                    if stats.last_message_at
                    else "never",
                    stats.recording_ok,
                )
    finally:
        recorder.stop()
    return 0


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - exercised by hand, not by pytest
    """Standalone recorder: capture a session with no UI in the way."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Record the OpenF1 live stream to disk.")
    parser.add_argument(
        "--force-live",
        action="store_true",
        help="record even if LIVE_MODE=false (also: RECORDER_FORCE_LIVE=1)",
    )
    args = parser.parse_args(argv)
    force = args.force_live or os.environ.get("RECORDER_FORCE_LIVE", "").lower() in ("1", "true", "yes")

    finished = threading.Event()

    def shutdown(signum: int, _frame: Any) -> None:
        log.info("signal %s received, shutting down", signum)
        finished.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    return run_standalone(get_settings(), force=force, stop_event=finished)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
