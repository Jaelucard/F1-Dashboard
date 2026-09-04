"""Replay a recorded session through the live adapter.

A recording is ``recordings/<session_key>/<topic>.jsonl`` as written by
``recorder.py``: one JSON object per line with ``received_at``, ``topic`` and
``payload``. Replay streams those lines back in ``received_at`` order into a
fresh ``OpenF1LiveSource``, so a replayed snapshot goes through exactly the
same validation, merging and session scoping as a live one.

Three units:

* ``RecordingReader`` - k-way merge of the topic files by ``received_at``.
  Memory is one line per open file however long the session was.
* ``RecordingCatalog`` - what is on disk, from the first and last line of each
  file only. Never loads a recording.
* ``ReplayPlayer`` - a thread that paces messages by the recorded gaps divided
  by the playback speed, with pause, seek and speed control.

Nothing here ever exposes an absolute path: errors say "unknown session" and
``ReplayInfo`` carries state, times and counters only.
"""

from __future__ import annotations

import heapq
import itertools
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

from pydantic import BaseModel

from app.adapters.openf1_live import OpenF1LiveSource
from app.config import Settings
from app.models import ReplayInfo, ReplayState, SessionState

log = logging.getLogger("app.replay")

MAX_SESSION_KEY = 10**12
SPEED_MIN = 0.25
SPEED_MAX = 50.0
MAX_GAP_SECONDS = 5.0
"""A gap in the recording longer than this (a red flag, a pause between
sessions) is replayed as this long, so the replay never appears frozen."""
TAIL_CHUNK = 64 * 1024
TOPIC_SESSIONS = "v1/sessions"


class ReplayError(RuntimeError):
    """Raised for an unknown session or an invalid control request."""


# -- lines ---------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayMessage:
    received_at: datetime
    topic: str
    payload: Any


def parse_line(line: str) -> ReplayMessage | None:
    """Decode one recorded line. None if it cannot be timed and routed."""
    text = line.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    stamp = obj.get("received_at")
    topic = obj.get("topic")
    if not isinstance(stamp, str) or not isinstance(topic, str):
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return ReplayMessage(received_at=when, topic=topic, payload=obj.get("payload"))


def _iso(when: datetime | None) -> str | None:
    return when.isoformat(timespec="milliseconds") if when else None


# -- reader --------------------------------------------------------------------


class RecordingReader:
    """Iterate a recording directory in ``received_at`` order.

    Each topic file is already in write order, so this is a k-way merge with a
    heap holding one pending line per file.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._files: list[IO[str]] = []
        self._heap: list[tuple[datetime, int, int, ReplayMessage]] = []
        self._counter = itertools.count()
        self._opened = False
        self.skipped_lines = 0
        """Non-blank lines that could not be parsed, for the current pass."""

    def _open(self) -> None:
        self.close()
        self.skipped_lines = 0
        for index, path in enumerate(sorted(self._directory.glob("*.jsonl"))):
            self._files.append(path.open("r", encoding="utf-8"))
            self._push(index)
        self._opened = True

    def _push(self, index: int) -> None:
        handle = self._files[index]
        while True:
            line = handle.readline()
            if not line:
                return
            if not line.strip():
                continue
            message = parse_line(line)
            if message is None:
                self.skipped_lines += 1
                continue
            heapq.heappush(self._heap, (message.received_at, index, next(self._counter), message))
            return

    def __iter__(self) -> RecordingReader:
        return self

    def __next__(self) -> ReplayMessage:
        if not self._opened:
            self._open()
        if not self._heap:
            raise StopIteration
        _, index, _, message = heapq.heappop(self._heap)
        self._push(index)
        return message

    def reset(self) -> None:
        """Start over from the first line of every file."""
        self._open()

    def close(self) -> None:
        for handle in self._files:
            try:
                handle.close()
            except OSError:
                pass
        self._files = []
        self._heap = []
        self._opened = False


# -- catalog -------------------------------------------------------------------


class RecordingSummary(BaseModel):
    """One recording on disk, as listed by ``GET /replay/sessions``."""

    session_key: int
    topics: dict[str, int]
    """Topic name to file size in bytes."""
    size_bytes: int
    start: str | None = None
    end: str | None = None
    session_name: str | None = None
    circuit_short_name: str | None = None


def resolve_session_dir(root: Path, session_key: int) -> Path:
    """The directory for ``session_key`` under ``root``, or ``ReplayError``.

    The key must be a plain non-negative int below ``MAX_SESSION_KEY`` and the
    directory must really live inside ``root`` (symlinks are resolved for the
    check only). The error never names a path.
    """
    if isinstance(session_key, bool) or not isinstance(session_key, int):
        raise ReplayError("unknown session")
    if session_key < 0 or session_key >= MAX_SESSION_KEY:
        raise ReplayError("unknown session")
    candidate = root / str(session_key)
    try:
        resolved = candidate.resolve(strict=True)
        root_resolved = root.resolve(strict=True)
    except OSError as exc:
        raise ReplayError("unknown session") from exc
    if not resolved.is_dir() or root_resolved not in resolved.parents:
        raise ReplayError("unknown session")
    return candidate


def _first_line(path: Path) -> str | None:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                return line
    return None


def _last_line(path: Path) -> str | None:
    """The last non-blank line, reading backwards in chunks."""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        buffer = b""
        while position > 0:
            step = min(TAIL_CHUNK, position)
            position -= step
            handle.seek(position)
            buffer = handle.read(step) + buffer
            lines = buffer.split(b"\n")
            complete = lines[1:] if position > 0 else lines
            candidates = [line for line in complete if line.strip()]
            if candidates:
                return candidates[-1].decode("utf-8", errors="replace")
    return None


class RecordingCatalog:
    """What recordings exist, from the ends of each file only."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def sessions(self) -> list[RecordingSummary]:
        if not self._root.is_dir():
            return []
        out: list[RecordingSummary] = []
        for child in self._root.iterdir():
            if not (child.is_dir() and child.name.isdigit() and len(child.name) <= 12):
                continue
            try:
                out.append(self._summarise(child, int(child.name)))
            except OSError:
                log.warning("could not read recording %s", child.name)
        return sorted(out, key=lambda summary: summary.session_key, reverse=True)

    def summary(self, session_key: int) -> RecordingSummary:
        directory = resolve_session_dir(self._root, session_key)
        return self._summarise(directory, session_key)

    @staticmethod
    def _summarise(directory: Path, session_key: int) -> RecordingSummary:
        topics: dict[str, int] = {}
        size = 0
        start: datetime | None = None
        end: datetime | None = None
        session_name: Any = None
        circuit: Any = None
        for path in sorted(directory.glob("*.jsonl")):
            size_bytes = path.stat().st_size
            size += size_bytes
            first = parse_line(_first_line(path) or "")
            last = parse_line(_last_line(path) or "")
            topic = first.topic if first else path.stem
            topics[topic] = size_bytes
            if first and (start is None or first.received_at < start):
                start = first.received_at
            if last and (end is None or last.received_at > end):
                end = last.received_at
            if topic == TOPIC_SESSIONS and last and isinstance(last.payload, dict):
                session_name = last.payload.get("session_name")
                circuit = last.payload.get("circuit_short_name")
        return RecordingSummary(
            session_key=session_key,
            topics=topics,
            size_bytes=size,
            start=_iso(start),
            end=_iso(end),
            session_name=session_name if isinstance(session_name, str) else None,
            circuit_short_name=circuit if isinstance(circuit, str) else None,
        )


# -- player --------------------------------------------------------------------


AdapterFactory = Callable[[], OpenF1LiveSource]


class ReplayPlayer:
    """Feed one recording into a fresh adapter at a controllable pace.

    Runs a daemon thread. All control methods return immediately; the thread
    picks the change up at its next wake. A seek is asynchronous: the state is
    ``seeking`` until the target has been reached.
    """

    def __init__(
        self,
        settings: Settings,
        session_key: int,
        *,
        sleep: Callable[[float], None] | None = None,
        adapter_factory: AdapterFactory | None = None,
    ) -> None:
        self.session_key = session_key
        self._directory = resolve_session_dir(settings.recordings_dir, session_key)
        summary = RecordingCatalog(settings.recordings_dir).summary(session_key)
        self._start = datetime.fromisoformat(summary.start) if summary.start else None
        self._end = datetime.fromisoformat(summary.end) if summary.end else None

        self._adapter_factory: AdapterFactory = adapter_factory or (
            lambda: OpenF1LiveSource(credentials_present=settings.credentials_present)
        )
        self._adapter = self._adapter_factory()
        self._reader = RecordingReader(self._directory)
        self._sleep = sleep

        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._state: ReplayState = "loaded"
        self._resume_state: ReplayState = "paused"
        self._speed = 1.0
        self._position: datetime | None = None
        self._last_applied_at: datetime | None = None
        self._pending_seek: datetime | None = None
        self._held: ReplayMessage | None = None
        self.messages_replayed = 0
        self.ingest_errors = 0

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"replay-{session_key}", daemon=True)
        self._thread.start()

    # -- control --------------------------------------------------------------

    @property
    def state(self) -> ReplayState:
        with self._lock:
            return self._state

    @property
    def speed(self) -> float:
        with self._lock:
            return self._speed

    @property
    def position(self) -> datetime | None:
        with self._lock:
            return self._position

    def play(self) -> None:
        with self._cv:
            if self._state == "finished":
                self._rewind()
            if self._state == "seeking":
                self._resume_state = "playing"
            else:
                self._state = "playing"
                self._resume_state = "playing"
            self._cv.notify_all()

    def pause(self) -> None:
        with self._cv:
            if self._state == "seeking":
                self._resume_state = "paused"
            elif self._state in ("playing", "loaded"):
                self._state = "paused"
                self._resume_state = "paused"
            self._cv.notify_all()

    def set_speed(self, speed: float) -> None:
        if not (SPEED_MIN <= speed <= SPEED_MAX):
            raise ValueError(f"speed must be between {SPEED_MIN} and {SPEED_MAX}")
        with self._cv:
            self._speed = float(speed)
            self._cv.notify_all()

    def seek(self, *, position: datetime | None = None, fraction: float | None = None) -> None:
        if (position is None) == (fraction is None):
            raise ValueError("seek takes exactly one of position or fraction")
        if self._start is None or self._end is None:
            return  # empty recording: nothing to seek in
        if fraction is not None:
            target = self._start + (self._end - self._start) * min(max(fraction, 0.0), 1.0)
        else:
            assert position is not None
            if position.tzinfo is None:
                position = position.replace(tzinfo=timezone.utc)
            target = min(max(position, self._start), self._end)
        with self._cv:
            if self._state != "seeking":
                self._resume_state = "playing" if self._state == "playing" else "paused"
            self._state = "seeking"
            self._pending_seek = target
            self._cv.notify_all()

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        self._thread.join(timeout=5)
        self._reader.close()

    # -- reporting ----------------------------------------------------------------

    def info(self) -> ReplayInfo:
        with self._lock:
            progress = 0.0
            if self._start is not None and self._end is not None and self._position is not None:
                span = (self._end - self._start).total_seconds()
                progress = 1.0 if span <= 0 else min(max((self._position - self._start).total_seconds() / span, 0.0), 1.0)
            return ReplayInfo(
                session_key=self.session_key,
                state=self._state,
                speed=self._speed,
                position=_iso(self._position),
                start=_iso(self._start),
                end=_iso(self._end),
                progress=progress,
                messages_replayed=self.messages_replayed,
                skipped_lines=self._reader.skipped_lines,
                ingest_errors=self.ingest_errors,
            )

    @property
    def adapter(self) -> OpenF1LiveSource:
        """The adapter this recording is being fed into.

        Exposed so ``backfill.py`` can fill in a recording that captured the
        timing topics but not the one-shot v1/sessions and v1/drivers
        announcements - which is every recording started mid-session.
        """
        with self._lock:
            return self._adapter

    def snapshot(self) -> SessionState:
        with self._lock:
            adapter = self._adapter
        state = adapter.snapshot(mode="replay")
        state.replay = self.info()
        return state

    # -- the thread -----------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._cv:
                while not self._stop.is_set() and self._pending_seek is None and self._state != "playing":
                    self._cv.wait(0.25)
                if self._stop.is_set():
                    return
                target = self._pending_seek
                self._pending_seek = None
            if target is not None:
                self._perform_seek(target)
                continue

            message = self._take_next()
            if message is None:
                with self._cv:
                    self._state = "finished"
                    self._position = self._end
                continue

            delay = self._delay_before(message)
            if delay is not None:
                self._wait(delay)
            with self._cv:
                if self._state != "playing" or self._pending_seek is not None:
                    self._held = message  # apply once we are playing again
                    continue
            self._apply(message)

    def _take_next(self) -> ReplayMessage | None:
        if self._held is not None:
            message, self._held = self._held, None
            return message
        try:
            return next(self._reader)
        except StopIteration:
            return None

    def _delay_before(self, message: ReplayMessage) -> float | None:
        if self._last_applied_at is None:
            return None
        gap = (message.received_at - self._last_applied_at).total_seconds()
        return min(max(gap / self.speed, 0.0), MAX_GAP_SECONDS)

    def _wait(self, seconds: float) -> None:
        if self._sleep is not None:
            self._sleep(seconds)
            return
        with self._cv:
            self._cv.wait(seconds)

    def _apply(self, message: ReplayMessage) -> None:
        try:
            self._adapter.ingest(message.topic, message.payload)
        except Exception:  # noqa: BLE001 - one bad message must not end the replay
            self.ingest_errors += 1
            if self.ingest_errors == 1 or self.ingest_errors % 100 == 0:
                log.exception("adapter raised on a replayed %s message (%d so far)", message.topic, self.ingest_errors)
        else:
            self.messages_replayed += 1
        with self._lock:
            self._position = message.received_at
            self._last_applied_at = message.received_at

    def _rewind(self) -> None:
        """Fresh adapter and reader; must be called with the lock held."""
        self._adapter = self._adapter_factory()
        self._reader.reset()
        self._held = None
        self._position = None
        self._last_applied_at = None
        self.messages_replayed = 0
        self.ingest_errors = 0

    def _perform_seek(self, target: datetime) -> None:
        with self._cv:
            if self._position is None or target < self._position:
                self._rewind()
        finished = False
        while True:
            with self._lock:
                if self._pending_seek is not None or self._stop.is_set():
                    break  # a newer seek supersedes this one
            message = self._take_next()
            if message is None:
                finished = True
                break
            if message.received_at > target:
                self._held = message
                break
            self._apply(message)
        with self._cv:
            if self._pending_seek is not None:
                return
            self._position = self._end if finished else target
            self._last_applied_at = self._position
            self._state = "finished" if finished else self._resume_state
            self._cv.notify_all()
