"""Turns the live MQTT stream into a SessionState.

Subscribes to the recorder in-process. It never opens its own MQTT connection,
which is both what the OpenF1 docs ask for and what keeps a bug in here from
costing us a recording: the recorder writes to disk first and only then calls
us, with our exceptions caught on its side.

Rules that drive the storage design:

* **Everything is scoped by ``session_key``.** OpenF1 tags every payload with
  the session it belongs to. Records live in a per-session bucket, only the
  *active* session is ever rendered, and a late message from an older session
  can never appear on the new session's board. The active session is chosen
  deterministically: the highest ``session_key`` that has produced *timing*
  data. A ``v1/sessions`` or ``v1/drivers`` announcement for a future session
  is stored but does not switch, because OpenF1 can publish the weekend's
  schedule before a session starts.
* **Validate before merging.** Each topic has a small schema. A record missing
  its identity fields (``driver_number`` and ``lap_number`` for a lap, and so
  on) is quarantined and counted; a wrong-typed non-identity field is dropped
  from the record and counted. Nothing malformed reaches a pydantic model, and
  a bad record cannot merge into a shared ``None:None`` key.
* **Upsert by ``_key``, never append.** The same ``_key`` on the same topic is
  an *update to the same record*. This is routine on ``v1/laps``, where a lap
  first arrives with only sector 1 and is revised as the rest fill in.
* **``_id`` is an ever-increasing message ordinal.** Where two messages carry
  the same key, the higher ``_id`` wins, so out-of-order delivery cannot
  overwrite fresh data with stale.
* **Telemetry is bounded.** ``location`` and ``car_data`` (and ``position`` and
  ``intervals``) carry a fresh ``_key`` on every message, so keying them by
  ``_key`` would retain the whole race in memory. They are keyed by driver
  instead: the latest sample drives the leaderboard and a short ring buffer
  per driver keeps recent history for the track map. The full history lives
  in the recording on disk. Every other store has a hard cap as well, so
  snapshot construction time stays flat however long the session runs.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from app import PHASE
from app.adapters.base import Record, SessionDataSource
from app.models import (
    AdapterInfo,
    DriverState,
    RaceControlMessage,
    RecorderInfo,
    SessionInfo,
    SessionState,
)

log = logging.getLogger(__name__)

# Topic names mirror the REST paths (confirmed on openf1.org/auth.html).
TOPIC_DRIVERS = "v1/drivers"
TOPIC_SESSIONS = "v1/sessions"
TOPIC_POSITION = "v1/position"
TOPIC_INTERVALS = "v1/intervals"
TOPIC_LAPS = "v1/laps"
TOPIC_STINTS = "v1/stints"
TOPIC_PIT = "v1/pit"
TOPIC_LOCATION = "v1/location"
TOPIC_CAR_DATA = "v1/car_data"
TOPIC_RACE_CONTROL = "v1/race_control"
TOPIC_TEAM_RADIO = "v1/team_radio"

KNOWN_TOPICS = frozenset({
    TOPIC_DRIVERS, TOPIC_SESSIONS, TOPIC_POSITION, TOPIC_INTERVALS, TOPIC_LAPS,
    TOPIC_STINTS, TOPIC_PIT, TOPIC_LOCATION, TOPIC_CAR_DATA, TOPIC_RACE_CONTROL,
    TOPIC_TEAM_RADIO,
})

# Latest-per-driver topics: a new message is a fresh sample, not a revision.
TELEMETRY_TOPICS = frozenset({TOPIC_POSITION, TOPIC_INTERVALS, TOPIC_LOCATION, TOPIC_CAR_DATA})

# Topics whose arrival proves a session is actually running. Announcements
# (sessions, drivers) do not count, or the next session's schedule would
# switch the board away from the one in progress.
TIMING_TOPICS = frozenset({
    TOPIC_POSITION, TOPIC_INTERVALS, TOPIC_LAPS, TOPIC_STINTS, TOPIC_PIT,
    TOPIC_LOCATION, TOPIC_CAR_DATA, TOPIC_RACE_CONTROL, TOPIC_TEAM_RADIO,
})

# Bounds. All hard caps, so memory and snapshot time are flat for a session.
TELEMETRY_HISTORY_PER_DRIVER = 64
MAX_RETAINED_SESSIONS = 2
KEYED_STORE_LIMIT = 5000
RACE_CONTROL_STORE_LIMIT = 500
TEAM_RADIO_STORE_LIMIT = 200
UNKNOWN_TOPIC_LIMIT = 200

# How many race control messages to keep for the status strip.
RACE_CONTROL_LIMIT = 60

FLAG_CLEARING = {"CLEAR", "GREEN"}

# Fields that carry no session information on their own.
META_FIELDS = frozenset({"_key", "_id", "session_key", "meeting_key"})


# -- validation ---------------------------------------------------------------

# Field kinds: int (bool excluded, digit strings coerced), number, str, bool,
# gap (float | "+1 LAP" | null), list. Every kind admits null - OpenF1 sends
# nulls routinely for values not yet known. Unlisted fields pass through
# untouched: OpenF1 adds fields, and stripping them would be inventing a
# schema we do not control.
_COMMON: dict[str, str] = {"_id": "int", "session_key": "int", "meeting_key": "int"}

FIELD_SCHEMAS: dict[str, dict[str, str]] = {
    TOPIC_DRIVERS: {
        **_COMMON, "driver_number": "int", "name_acronym": "str", "full_name": "str",
        "broadcast_name": "str", "first_name": "str", "last_name": "str", "team_name": "str",
        "team_colour": "str", "country_code": "str", "headshot_url": "str",
    },
    TOPIC_SESSIONS: {
        **_COMMON, "circuit_key": "int", "country_key": "int", "year": "int",
        "session_name": "str", "session_type": "str", "circuit_short_name": "str",
        "country_name": "str", "country_code": "str", "location": "str",
        "date_start": "str", "date_end": "str", "gmt_offset": "str",
    },
    TOPIC_POSITION: {**_COMMON, "driver_number": "int", "position": "int", "date": "str"},
    TOPIC_INTERVALS: {
        **_COMMON, "driver_number": "int", "gap_to_leader": "gap", "interval": "gap", "date": "str",
    },
    TOPIC_LAPS: {
        **_COMMON, "driver_number": "int", "lap_number": "int", "lap_duration": "number",
        "duration_sector_1": "number", "duration_sector_2": "number", "duration_sector_3": "number",
        "i1_speed": "number", "i2_speed": "number", "st_speed": "number",
        "is_pit_out_lap": "bool", "date_start": "str",
        "segments_sector_1": "list", "segments_sector_2": "list", "segments_sector_3": "list",
    },
    TOPIC_STINTS: {
        **_COMMON, "driver_number": "int", "stint_number": "int", "lap_start": "int",
        "lap_end": "int", "compound": "str", "tyre_age_at_start": "int",
    },
    TOPIC_PIT: {
        **_COMMON, "driver_number": "int", "lap_number": "int", "date": "str",
        "pit_duration": "number", "lane_duration": "number", "stop_duration": "number",
    },
    TOPIC_LOCATION: {**_COMMON, "driver_number": "int", "x": "number", "y": "number", "z": "number", "date": "str"},
    TOPIC_CAR_DATA: {
        **_COMMON, "driver_number": "int", "speed": "number", "rpm": "number", "n_gear": "int",
        "throttle": "number", "brake": "number", "drs": "int", "date": "str",
    },
    TOPIC_RACE_CONTROL: {
        **_COMMON, "date": "str", "category": "str", "flag": "str", "scope": "str",
        "sector": "int", "lap_number": "int", "driver_number": "int", "message": "str",
    },
    TOPIC_TEAM_RADIO: {**_COMMON, "driver_number": "int", "date": "str", "recording_url": "str"},
}

# Fields that must be present and non-null for a record to have an identity.
# Any one of the tuples in the list is enough.
IDENTITY_FIELDS: dict[str, list[tuple[str, ...]]] = {
    TOPIC_DRIVERS: [("driver_number",)],
    TOPIC_SESSIONS: [("session_key",)],
    TOPIC_POSITION: [("driver_number",)],
    TOPIC_INTERVALS: [("driver_number",)],
    TOPIC_LOCATION: [("driver_number",)],
    TOPIC_CAR_DATA: [("driver_number",)],
    TOPIC_LAPS: [("driver_number", "lap_number")],
    TOPIC_STINTS: [("driver_number", "stint_number")],
    TOPIC_PIT: [("driver_number", "lap_number"), ("driver_number", "date")],
    TOPIC_RACE_CONTROL: [("_key",), ("date",)],
    TOPIC_TEAM_RADIO: [("driver_number", "date"), ("_key",)],
}


def _coerce(kind: str, value: Any) -> tuple[bool, Any]:
    """(ok, normalised value). Null is acceptable for every kind."""
    if value is None:
        return True, None
    if isinstance(value, bool):
        return kind == "bool", value
    if kind == "int":
        if isinstance(value, int):
            return True, value
        if isinstance(value, float) and value.is_integer():
            return True, int(value)
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return True, int(value.strip())
        return False, None
    if kind == "number":
        return isinstance(value, (int, float)), value
    if kind == "str":
        return isinstance(value, str), value
    if kind == "bool":
        return False, None
    if kind == "gap":
        return isinstance(value, (int, float, str)), value
    if kind == "list":
        return isinstance(value, list), value
    return True, value


def validate_record(topic: str, payload: Any) -> tuple[Record | None, list[str]]:
    """Normalise one payload for one topic.

    Returns ``(record, dropped_fields)``. ``record`` is None when the payload
    is not a dict or has no usable identity; then ``dropped_fields`` holds the
    reason. Otherwise wrong-typed known fields are removed and listed.
    """
    if not isinstance(payload, dict):
        return None, ["not an object"]

    record: Record = {}
    dropped: list[str] = []
    schema = FIELD_SCHEMAS.get(topic, _COMMON)

    for name, value in payload.items():
        if not isinstance(name, str):
            dropped.append(str(name))
            continue
        if name == "_key":
            if value is None:
                continue
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
                record["_key"] = str(value)
            else:
                dropped.append("_key")
            continue
        kind = schema.get(name)
        if kind is None:
            record[name] = value
            continue
        ok, normalised = _coerce(kind, value)
        if ok:
            record[name] = normalised
        else:
            dropped.append(name)

    requirements = IDENTITY_FIELDS.get(topic)
    if requirements is not None:
        if not any(all(record.get(f) is not None for f in option) for option in requirements):
            return None, ["missing identity: " + " or ".join("+".join(o) for o in requirements)]

    return record, dropped


def is_empty_telemetry(topic: str, record: Record) -> bool:
    """A telemetry sample whose only surviving fields are identity and meta.

    Applying it would replace a good reading with nothing, so it is quarantined
    instead. Only telemetry: a lap with just driver and lap number is a real
    event (a new lap has started).
    """
    return topic in TELEMETRY_TOPICS and not any(
        name not in META_FIELDS and name != "driver_number" for name in record
    )


def natural_key(topic: str, payload: Record) -> str | None:
    """A stable identity for a record when the message carries no `_key`.

    Chosen per topic so that the record which OpenF1 would revise in place maps
    to the same key here: a lap is identified by driver and lap number, a stint
    by driver and stint number, and so on. Returns None when the fields that
    make up the identity are missing - such a record must be rejected, never
    merged into a shared "None:None" bucket.
    """
    driver = payload.get("driver_number")
    if topic == TOPIC_LAPS:
        lap = payload.get("lap_number")
        return f"{driver}:{lap}" if driver is not None and lap is not None else None
    if topic == TOPIC_STINTS:
        stint = payload.get("stint_number")
        return f"{driver}:{stint}" if driver is not None and stint is not None else None
    if topic == TOPIC_PIT:
        lap, date = payload.get("lap_number"), payload.get("date")
        if driver is None or (lap is None and date is None):
            return None
        return f"{driver}:{lap}:{date}"
    if topic in TELEMETRY_TOPICS or topic == TOPIC_DRIVERS:
        # Latest-wins per driver.
        return str(driver) if driver is not None else None
    if topic == TOPIC_SESSIONS:
        key = payload.get("session_key")
        return str(key) if key is not None else None
    date = payload.get("date")
    if date is None:
        return None
    return f"{date}:{payload.get('message', '')}"


def record_key(topic: str, payload: Record) -> str | None:
    """The identity a record is stored under.

    Telemetry topics are always keyed by driver: their `_key` is unique per
    message, and honouring it would retain every sample of the session.
    """
    if topic in TELEMETRY_TOPICS:
        return natural_key(topic, payload)
    key = payload.get("_key")
    if key is not None and str(key).strip():
        return str(key)
    return natural_key(topic, payload)


def compute_tyre_age(
    current_lap: int | None, lap_start: int | None, tyre_age_at_start: int | None
) -> int | None:
    """Laps on the current set: current lap - stint start + age when fitted.

    On the first lap of a stint this is exactly ``tyre_age_at_start``, which is
    0 for a new set and non-zero for a scrubbed one. Never negative: a stint
    record can arrive before the lap count catches up.
    """
    if lap_start is None:
        return None
    age_at_start = tyre_age_at_start or 0
    if current_lap is None:
        return age_at_start
    return max(0, current_lap - lap_start + age_at_start)


def _as_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_DATETIME_MIN = datetime.min.replace(tzinfo=timezone.utc)


def _time_order_key(record: Record, field_name: str = "date") -> tuple[int, datetime, int]:
    """Sort key: parsed UTC time, then `_id`. Unparseable times sort first,
    so a record with a broken timestamp can never become "the latest"."""
    stamp = _as_datetime(record.get(field_name))
    message_id = record.get("_id")
    return (
        0 if stamp is None else 1,
        stamp or _DATETIME_MIN,
        message_id if isinstance(message_id, int) else -1,
    )


def _filter_since(records: list[Record], since: datetime | None, field_name: str) -> list[Record]:
    if since is None:
        return records
    kept = []
    for record in records:
        stamp = _as_datetime(record.get(field_name))
        if stamp is None or stamp >= since:
            kept.append(record)
    return kept


# -- stores -------------------------------------------------------------------


class TopicStore:
    """Records for one topic, upserted by key, merged, ordered by `_id`, capped.

    Optionally maintains a secondary index by ``driver_number`` so snapshot
    construction does not rescan the whole store every second.
    """

    def __init__(
        self,
        topic: str,
        *,
        limit: int = KEYED_STORE_LIMIT,
        group_by: str | None = None,
    ) -> None:
        self.topic = topic
        self._limit = limit
        self._group_by = group_by
        self._records: OrderedDict[str, Record] = OrderedDict()
        self._ids: dict[str, int] = {}
        self._groups: dict[int, OrderedDict[str, Record]] = {}
        self._sequence = 0
        self.updates = 0
        self.inserts = 0
        self.stale_drops = 0
        self.duplicates = 0
        self.evicted = 0

    def upsert(self, payload: Record) -> bool:
        """Apply a message. Returns False if it was stale and ignored."""
        key = record_key(self.topic, payload)
        if key is None:
            # Unknown topics have no natural key; keep them in arrival order.
            self._sequence += 1
            key = f"#{self._sequence}"
        message_id = payload.get("_id")

        if key in self._records:
            previous_id = self._ids.get(key)
            if isinstance(message_id, int) and isinstance(previous_id, int):
                if message_id < previous_id:
                    self.stale_drops += 1
                    return False
                if message_id == previous_id:
                    self.duplicates += 1
            # Merge rather than replace: a revision may carry only the fields
            # that changed, and dropping the rest would blank the leaderboard.
            merged = {**self._records[key], **payload}
            self._records[key] = merged
            self.updates += 1
        else:
            merged = dict(payload)
            self._records[key] = merged
            self.inserts += 1
            if len(self._records) > self._limit:
                old_key, old_record = self._records.popitem(last=False)
                self._ids.pop(old_key, None)
                self._unindex(old_key, old_record)
                self.evicted += 1

        if isinstance(message_id, int):
            self._ids[key] = message_id
        self._index(key, merged)
        return True

    def _index(self, key: str, record: Record) -> None:
        if self._group_by is None:
            return
        number = record.get(self._group_by)
        if isinstance(number, int):
            self._groups.setdefault(number, OrderedDict())[key] = record

    def _unindex(self, key: str, record: Record) -> None:
        if self._group_by is None:
            return
        number = record.get(self._group_by)
        group = self._groups.get(number)  # type: ignore[arg-type]
        if group is not None:
            group.pop(key, None)
            if not group:
                self._groups.pop(number, None)  # type: ignore[arg-type]

    def all(self) -> list[Record]:
        return list(self._records.values())

    def by_driver(self) -> dict[int, Record]:
        out: dict[int, Record] = {}
        for record in self._records.values():
            number = record.get("driver_number")
            if isinstance(number, int):
                out[number] = record
        return out

    def grouped(self) -> dict[int, list[Record]]:
        return {number: list(group.values()) for number, group in self._groups.items()}

    def __len__(self) -> int:
        return len(self._records)


class LatestPerDriverStore:
    """Bounded telemetry: the latest sample per driver plus a short history.

    Samples replace rather than merge - each message is a complete reading -
    and an older sample (by `_id`, or by `date` when there is no `_id`) can
    never displace a newer one.
    """

    def __init__(self, topic: str, *, history: int = TELEMETRY_HISTORY_PER_DRIVER) -> None:
        self.topic = topic
        self._history = history
        self._samples: dict[int, deque[Record]] = {}
        self.inserts = 0
        self.updates = 0
        self.stale_drops = 0
        self.duplicates = 0
        self.evicted = 0

    def upsert(self, payload: Record) -> bool:
        number = payload.get("driver_number")
        if not isinstance(number, int):
            return False
        samples = self._samples.get(number)
        if samples is None:
            samples = deque(maxlen=self._history)
            self._samples[number] = samples
            self.inserts += 1
        elif samples:
            latest = samples[-1]
            order = self._compare(payload, latest)
            if order < 0:
                self.stale_drops += 1
                return False
            if order == 0 and payload.get("_id") is not None:
                self.duplicates += 1
            self.updates += 1
        if len(samples) == samples.maxlen:
            self.evicted += 1
        samples.append(dict(payload))
        return True

    @staticmethod
    def _compare(new: Record, old: Record) -> int:
        new_id, old_id = new.get("_id"), old.get("_id")
        if isinstance(new_id, int) and isinstance(old_id, int):
            return (new_id > old_id) - (new_id < old_id)
        new_date, old_date = _as_datetime(new.get("date")), _as_datetime(old.get("date"))
        if new_date is not None and old_date is not None:
            return (new_date > old_date) - (new_date < old_date)
        return 1

    def all(self) -> list[Record]:
        out: list[Record] = []
        for number in sorted(self._samples):
            out.extend(self._samples[number])
        return out

    def by_driver(self) -> dict[int, Record]:
        return {number: samples[-1] for number, samples in self._samples.items() if samples}

    def __len__(self) -> int:
        return sum(len(samples) for samples in self._samples.values())


Store = TopicStore | LatestPerDriverStore


def _new_store(topic: str) -> Store:
    if topic in (TOPIC_LOCATION, TOPIC_CAR_DATA):
        return LatestPerDriverStore(topic)
    if topic in TELEMETRY_TOPICS:
        # Running order and gaps: only the latest reading has any use.
        return LatestPerDriverStore(topic, history=1)
    if topic in (TOPIC_LAPS, TOPIC_STINTS, TOPIC_PIT):
        return TopicStore(topic, group_by="driver_number")
    if topic == TOPIC_RACE_CONTROL:
        return TopicStore(topic, limit=RACE_CONTROL_STORE_LIMIT)
    if topic == TOPIC_TEAM_RADIO:
        return TopicStore(topic, limit=TEAM_RADIO_STORE_LIMIT)
    if topic in KNOWN_TOPICS:
        return TopicStore(topic)
    return TopicStore(topic, limit=UNKNOWN_TOPIC_LIMIT)


@dataclass
class SessionBucket:
    """Everything received for one ``session_key``."""

    key: int | None
    stores: dict[str, Store] = field(default_factory=dict)
    timing_seen: bool = False

    def store(self, topic: str) -> Store:
        store = self.stores.get(topic)
        if store is None:
            store = _new_store(topic)
            self.stores[topic] = store
        return store


# -- the source ---------------------------------------------------------------


class OpenF1LiveSource(SessionDataSource):
    """In-memory merge of the live stream into one SessionState."""

    def __init__(
        self,
        *,
        credentials_present: bool = False,
        max_sessions: int = MAX_RETAINED_SESSIONS,
        on_session_switch: Callable[[int | None, int], None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._credentials_present = credentials_present
        self._max_sessions = max(1, max_sessions)
        self._on_session_switch = on_session_switch

        self._buckets: dict[int | None, SessionBucket] = {}
        self._active_key: int | None = None

        self._messages_seen = 0
        self._last_message_at: datetime | None = None
        self._unknown_topics: set[str] = set()
        self._quarantined = 0
        self._quarantine_reasons: dict[str, int] = {}
        self._malformed_fields = 0
        self._late_session_messages = 0
        self._session_switches = 0
        self._driver_build_errors = 0

    # -- ingest -------------------------------------------------------------

    def ingest(self, topic: str, payload: Any) -> bool:
        """Apply one message. Safe to call from the recorder's thread.

        Returns True if the record was applied to some session's store.
        """
        with self._lock:
            self._messages_seen += 1
            self._last_message_at = datetime.now(timezone.utc)

            if topic not in KNOWN_TOPICS and topic not in self._unknown_topics:
                # Record it anyway - an unexpected topic is data we should not
                # throw away - but log the name once so Friday's notes are easy.
                self._unknown_topics.add(topic)
                log.info("receiving an unlisted topic: %r", topic)

            record, dropped = validate_record(topic, payload)
            if record is not None and is_empty_telemetry(topic, record):
                self._malformed_fields += len(dropped)
                record, dropped = None, ["empty telemetry record"]
            if record is None:
                self._quarantined += 1
                reason = f"{topic}: {dropped[0]}" if dropped else topic
                self._quarantine_reasons[reason] = self._quarantine_reasons.get(reason, 0) + 1
                return False
            self._malformed_fields += len(dropped)

            bucket = self._bucket_for(topic, record)
            if bucket is None:
                return False
            return bucket.store(topic).upsert(record)

    def on_raw_message(self, message: Any) -> None:
        """Subscriber callback for RawRecorder.add_subscriber."""
        self.ingest(message.topic, message.payload)

    def _bucket_for(self, topic: str, record: Record) -> SessionBucket | None:
        key = record.get("session_key")
        session_key: int | None = key if isinstance(key, int) else self._active_key

        bucket = self._buckets.get(session_key)
        is_older = (
            session_key is not None
            and self._active_key is not None
            and session_key < self._active_key
        )
        if bucket is None:
            if is_older:
                # Never seen, or already evicted: a straggler from a session
                # that is over. Dropped, counted, never shown.
                self._late_session_messages += 1
                return None
            bucket = SessionBucket(key=session_key)
            self._buckets[session_key] = bucket
            self._evict_sessions()
        elif is_older:
            # Retained older session: applied there, so it cannot touch the
            # active board, but counted so the operator can see it happening.
            self._late_session_messages += 1

        if topic in TIMING_TOPICS:
            bucket.timing_seen = True
            if session_key is not None and (
                self._active_key is None or session_key > self._active_key
            ):
                self._switch_to(session_key)
        return bucket

    def _switch_to(self, session_key: int) -> None:
        previous = self._active_key
        self._active_key = session_key
        if previous is not None:
            self._session_switches += 1
            log.warning("active session changed: %s -> %s", previous, session_key)
        else:
            log.info("active session: %s", session_key)
        self._evict_sessions()
        if self._on_session_switch is not None:
            try:
                self._on_session_switch(previous, session_key)
            except Exception:  # noqa: BLE001
                log.exception("session switch hook raised")

    def _evict_sessions(self) -> None:
        while len(self._buckets) > self._max_sessions:
            candidates = [k for k in self._buckets if k != self._active_key]
            if not candidates:
                return
            # None (unscoped) sorts lowest; otherwise the smallest key is oldest.
            victim = min(candidates, key=lambda k: (k is not None, k if k is not None else 0))
            self._buckets.pop(victim, None)
            log.info("evicted retained session %s", victim)

    def _active(self) -> SessionBucket | None:
        return self._buckets.get(self._active_key)

    def _records(self, topic: str) -> list[Record]:
        bucket = self._active()
        if bucket is None:
            return []
        store = bucket.stores.get(topic)
        return store.all() if store is not None else []

    def _by_driver(self, topic: str) -> dict[int, Record]:
        bucket = self._active()
        if bucket is None:
            return {}
        store = bucket.stores.get(topic)
        return store.by_driver() if store is not None else {}

    def _grouped(self, topic: str) -> dict[int, list[Record]]:
        bucket = self._active()
        if bucket is None:
            return {}
        store = bucket.stores.get(topic)
        if isinstance(store, TopicStore):
            return store.grouped()
        return {}

    # -- SessionDataSource --------------------------------------------------

    def get_drivers(self) -> list[Record]:
        with self._lock:
            return self._records(TOPIC_DRIVERS)

    def get_locations(self, since: datetime | None = None) -> list[Record]:
        with self._lock:
            return _filter_since(self._records(TOPIC_LOCATION), since, "date")

    def get_positions(self, since: datetime | None = None) -> list[Record]:
        with self._lock:
            return _filter_since(self._records(TOPIC_POSITION), since, "date")

    def get_intervals(self, since: datetime | None = None) -> list[Record]:
        with self._lock:
            return _filter_since(self._records(TOPIC_INTERVALS), since, "date")

    def get_laps(self, since: datetime | None = None) -> list[Record]:
        with self._lock:
            return _filter_since(self._records(TOPIC_LAPS), since, "date_start")

    def get_stints(self) -> list[Record]:
        with self._lock:
            return self._records(TOPIC_STINTS)

    def get_pits(self, since: datetime | None = None) -> list[Record]:
        with self._lock:
            return _filter_since(self._records(TOPIC_PIT), since, "date")

    def get_team_radio(self, since: datetime | None = None) -> list[Record]:
        with self._lock:
            return _filter_since(self._records(TOPIC_TEAM_RADIO), since, "date")

    def get_race_control(self, since: datetime | None = None) -> list[Record]:
        with self._lock:
            return _filter_since(self._sorted_race_control(), since, "date")

    def get_car_data(self, since: datetime | None = None) -> list[Record]:
        with self._lock:
            return _filter_since(self._records(TOPIC_CAR_DATA), since, "date")

    # -- snapshot -----------------------------------------------------------

    def snapshot(
        self, *, mode: str = "live", recorder: RecorderInfo | None = None
    ) -> SessionState:
        with self._lock:
            drivers = self._build_drivers()
            session = self._build_session()
            flag, status, partial_aero = self._build_race_control_state()
            best = [d.best_lap_duration for d in drivers if d.best_lap_duration]

            return SessionState(
                phase=PHASE,
                mode=mode,  # type: ignore[arg-type]
                server_time=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                credentials_present=self._credentials_present,
                session=session,
                drivers=drivers,
                track_flag=flag,
                session_status=status,
                partial_aero=partial_aero,
                session_best_lap=min(best) if best else None,
                race_control=self._build_race_control_messages(),
                recorder=recorder,
                adapter=self._adapter_info(),
            )

    def _build_session(self) -> SessionInfo | None:
        records = self._records(TOPIC_SESSIONS)
        if not records:
            return None
        bucket = self._active()
        latest = records[-1]
        if bucket is not None and bucket.key is not None:
            for record in records:
                if record.get("session_key") == bucket.key:
                    latest = record
        try:
            return SessionInfo(
                **{k: latest.get(k) for k in SessionInfo.model_fields if k in latest}
            )
        except Exception:  # noqa: BLE001 - validated on ingest; belt and braces
            log.exception("could not build SessionInfo")
            return None

    def _build_drivers(self) -> list[DriverState]:
        entries = self._by_driver(TOPIC_DRIVERS)
        positions = self._by_driver(TOPIC_POSITION)
        intervals = self._by_driver(TOPIC_INTERVALS)
        car_data = self._by_driver(TOPIC_CAR_DATA)
        locations = self._by_driver(TOPIC_LOCATION)
        laps_by_driver = self._grouped(TOPIC_LAPS)
        stints_by_driver = self._grouped(TOPIC_STINTS)
        pits_by_driver = self._grouped(TOPIC_PIT)

        # A driver may appear in timing before the entry list arrives, so build
        # the roster from every topic that names a driver rather than from
        # v1/drivers alone. car_data and stints are included because either can
        # be the first thing we hear about a car that is already on track.
        numbers = (
            set(entries)
            | set(positions)
            | set(intervals)
            | set(laps_by_driver)
            | set(car_data)
            | set(stints_by_driver)
            | set(pits_by_driver)
        )

        states: list[DriverState] = []
        for number in numbers:
            try:
                states.append(
                    self._build_driver(
                        number,
                        entries.get(number, {}),
                        positions.get(number, {}),
                        intervals.get(number, {}),
                        car_data.get(number, {}),
                        locations.get(number, {}),
                        laps_by_driver.get(number, []),
                        stints_by_driver.get(number, []),
                        pits_by_driver.get(number, []),
                    )
                )
            except Exception:  # noqa: BLE001 - one bad driver must not blank the board
                self._driver_build_errors += 1
                log.exception("could not build state for driver %s", number)

        # Sort by position, with unclassified cars last but still shown.
        states.sort(key=lambda d: (d.position is None, d.position or 0, d.driver_number))

        # Interval to the car behind is just the following car's interval to
        # the car ahead - it is the same gap seen from the other side.
        for ahead, behind in zip(states, states[1:]):
            ahead.interval_behind = behind.interval_ahead

        return states

    @staticmethod
    def _build_driver(
        number: int,
        entry: Record,
        position: Record,
        interval: Record,
        car: Record,
        location: Record,
        laps: list[Record],
        stints: list[Record],
        pits: list[Record],
    ) -> DriverState:
        laps = sorted(laps, key=lambda lap: lap.get("lap_number") or 0)
        latest_lap = laps[-1] if laps else {}
        completed = [
            lap.get("lap_duration")
            for lap in laps
            if isinstance(lap.get("lap_duration"), (int, float))
        ]

        # "Last lap" means the most recent *completed* lap, not the lap in
        # progress, whose lap_duration is still null.
        last_completed = next(
            (lap for lap in reversed(laps) if isinstance(lap.get("lap_duration"), (int, float))),
            {},
        )

        stints = sorted(stints, key=lambda stint: stint.get("stint_number") or 0)
        stint = stints[-1] if stints else {}
        current_lap = latest_lap.get("lap_number")

        pit_laps = [pit.get("lap_number") for pit in pits if isinstance(pit.get("lap_number"), int)]
        latest_pit_lap = max(pit_laps) if pit_laps else None

        return DriverState(
            driver_number=number,
            name_acronym=entry.get("name_acronym"),
            full_name=entry.get("full_name"),
            broadcast_name=entry.get("broadcast_name"),
            team_name=entry.get("team_name"),
            team_colour=entry.get("team_colour"),
            position=position.get("position"),
            gap_to_leader=interval.get("gap_to_leader"),
            interval_ahead=interval.get("interval"),
            lap_number=current_lap,
            last_lap_duration=last_completed.get("lap_duration"),
            best_lap_duration=min(completed) if completed else None,
            sector_1=last_completed.get("duration_sector_1"),
            sector_2=last_completed.get("duration_sector_2"),
            sector_3=last_completed.get("duration_sector_3"),
            is_pit_out_lap=bool(latest_lap.get("is_pit_out_lap")),
            compound=stint.get("compound"),
            stint_number=stint.get("stint_number"),
            tyre_age=compute_tyre_age(
                current_lap, stint.get("lap_start"), stint.get("tyre_age_at_start")
            ),
            # Derived, not reported: OpenF1 has no "car is in the pit lane
            # right now" field. A pit record for the lap currently in progress
            # is the best available signal, and it clears once the driver
            # starts a new lap.
            in_pit=(current_lap is not None and latest_pit_lap == current_lap),
            pit_count=len(pits),
            aero_raw=car.get("drs"),
            speed=car.get("speed"),
            updated_at=interval.get("date") or position.get("date"),
            x=location.get("x"),
            y=location.get("y"),
            location_at=location.get("date"),
        )

    def _sorted_race_control(self) -> list[Record]:
        # Parsed UTC time, then _id, then arrival order (sorted() is stable).
        return sorted(self._records(TOPIC_RACE_CONTROL), key=_time_order_key)

    def _build_race_control_messages(self) -> list[RaceControlMessage]:
        recent = self._sorted_race_control()[-RACE_CONTROL_LIMIT:]
        out: list[RaceControlMessage] = []
        for record in recent:
            try:
                out.append(
                    RaceControlMessage(
                        **{k: record.get(k) for k in RaceControlMessage.model_fields if k in record}
                    )
                )
            except Exception:  # noqa: BLE001
                log.exception("could not build RaceControlMessage")
        return out

    def _build_race_control_state(self) -> tuple[str | None, str | None, bool]:
        """Current track flag, session status, and the 2026 partial-aero badge."""
        flag: str | None = None
        status: str | None = None
        partial_aero = False

        for record in self._sorted_race_control():
            category = record.get("category")
            if category == "Flag" and record.get("scope") == "Track":
                value = record.get("flag")
                flag = None if value in FLAG_CLEARING else value
            elif category == "SessionStatus":
                status = record.get("message")

            # 2026: race control may enable front-Straight/rear-Corner in the
            # wet. Detected from the message text because there is no dedicated
            # field, and deliberately conservative - it never guesses from the
            # legacy Drs category.
            message = str(record.get("message") or "").upper()
            if "AERO" in message:
                # Check DISABLED first: "PARTIAL AERO MODE DISABLED" contains
                # both words, and testing for enablement first would latch the
                # badge on permanently the moment it was switched off.
                if "DISABLED" in message or "DISABLE" in message:
                    partial_aero = False
                elif "ENABLED" in message or "PARTIAL" in message:
                    partial_aero = True

        return flag, status, partial_aero

    # -- introspection ------------------------------------------------------

    def _adapter_info(self) -> AdapterInfo:
        return AdapterInfo(
            active_session_key=self._active_key,
            sessions_retained=sorted(k for k in self._buckets if k is not None),
            messages_seen=self._messages_seen,
            quarantined_records=self._quarantined,
            malformed_fields=self._malformed_fields,
            late_session_messages=self._late_session_messages,
            session_switches=self._session_switches,
            driver_build_errors=self._driver_build_errors,
        )

    @property
    def active_session_key(self) -> int | None:
        return self._active_key

    def stats(self) -> dict[str, Any]:
        with self._lock:
            bucket = self._active()
            stores = bucket.stores if bucket is not None else {}
            return {
                "messages_seen": self._messages_seen,
                "last_message_at": (
                    self._last_message_at.isoformat(timespec="milliseconds")
                    if self._last_message_at
                    else None
                ),
                "active_session_key": self._active_key,
                "sessions": sorted(k for k in self._buckets if k is not None),
                "session_switches": self._session_switches,
                "quarantined_records": self._quarantined,
                "quarantine_reasons": dict(sorted(self._quarantine_reasons.items())),
                "malformed_fields": self._malformed_fields,
                "late_session_messages": self._late_session_messages,
                "driver_build_errors": self._driver_build_errors,
                "topics": {
                    topic: {
                        "records": len(store),
                        "inserts": store.inserts,
                        "updates": store.updates,
                        "stale_drops": store.stale_drops,
                        "duplicates": store.duplicates,
                        "evicted": store.evicted,
                    }
                    for topic, store in sorted(stores.items())
                },
            }
