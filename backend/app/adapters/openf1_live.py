"""Turns the live MQTT stream into a SessionState.

Subscribes to the recorder in-process. It never opens its own MQTT connection,
which is both what the OpenF1 docs ask for and what keeps a bug in here from
costing us a recording: the recorder writes to disk first and only then calls
us, with our exceptions caught on its side.

Two rules from the docs drive the storage design:

* **Upsert by `_key`, never append.** The same `_key` on the same topic is an
  *update to the same record*. This is routine on `v1/laps`, where a lap first
  arrives with only sector 1 filled in and is revised as sectors 2 and 3 come
  in. Appending would give a driver several partial copies of one lap and a
  "last lap" that flickers between them.
* **`_id` is an ever-increasing message ordinal.** Where two messages carry the
  same `_key`, the higher `_id` wins, so a message delivered out of order
  cannot overwrite fresher data with staler data.

Not every message has a `_key` - the REST payloads do not, and the replay and
historical adapters reuse this merge logic - so ``natural_key`` derives a
stable identity per topic when `_key` is missing.

Memory: only the *latest* location and car_data per driver is kept. A race is
roughly half a million location points; the full history lives in the recording
on disk, which is what `track.py` reads in Tier B. Everything else is small
enough to hold in full.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from app import PHASE
from app.adapters.base import Record, SessionDataSource
from app.models import (
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

# How many race control messages to keep for the status strip.
RACE_CONTROL_LIMIT = 60

FLAG_CLEARING = {"CLEAR", "GREEN"}


def natural_key(topic: str, payload: Record) -> str:
    """A stable identity for a record when the message carries no `_key`.

    Chosen per topic so that the record which OpenF1 would revise in place maps
    to the same key here: a lap is identified by driver and lap number, a stint
    by driver and stint number, and so on.
    """
    driver = payload.get("driver_number")
    if topic == TOPIC_LAPS:
        return f"{driver}:{payload.get('lap_number')}"
    if topic == TOPIC_STINTS:
        return f"{driver}:{payload.get('stint_number')}"
    if topic == TOPIC_PIT:
        return f"{driver}:{payload.get('lap_number')}:{payload.get('date')}"
    if topic in (TOPIC_DRIVERS, TOPIC_POSITION, TOPIC_INTERVALS, TOPIC_LOCATION, TOPIC_CAR_DATA):
        # Latest-wins per driver.
        return str(driver)
    if topic == TOPIC_SESSIONS:
        return str(payload.get("session_key"))
    return f"{payload.get('date')}:{payload.get('message', '')}"


def record_key(topic: str, payload: Record) -> str:
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


class TopicStore:
    """Records for one topic, upserted by key and ordered by `_id`."""

    def __init__(self, topic: str) -> None:
        self.topic = topic
        self._records: OrderedDict[str, Record] = OrderedDict()
        self._ids: dict[str, int] = {}
        self.updates = 0
        self.inserts = 0
        self.stale_drops = 0

    def upsert(self, payload: Record) -> bool:
        """Apply a message. Returns False if it was stale and ignored."""
        key = record_key(self.topic, payload)
        message_id = payload.get("_id")

        if key in self._records:
            previous_id = self._ids.get(key)
            if (
                isinstance(message_id, int)
                and isinstance(previous_id, int)
                and message_id < previous_id
            ):
                self.stale_drops += 1
                return False
            # Merge rather than replace: a revision may carry only the fields
            # that changed, and dropping the rest would blank the leaderboard.
            merged = {**self._records[key], **payload}
            self._records[key] = merged
            self.updates += 1
        else:
            self._records[key] = dict(payload)
            self.inserts += 1

        if isinstance(message_id, int):
            self._ids[key] = message_id
        return True

    def all(self) -> list[Record]:
        return list(self._records.values())

    def by_driver(self) -> dict[int, Record]:
        out: dict[int, Record] = {}
        for record in self._records.values():
            number = record.get("driver_number")
            if isinstance(number, int):
                out[number] = record
        return out

    def __len__(self) -> int:
        return len(self._records)


def _as_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _filter_since(records: list[Record], since: datetime | None, field: str) -> list[Record]:
    if since is None:
        return records
    kept = []
    for record in records:
        stamp = _as_datetime(record.get(field))
        if stamp is None or stamp >= since:
            kept.append(record)
    return kept


class OpenF1LiveSource(SessionDataSource):
    """In-memory merge of the live stream into one SessionState."""

    def __init__(self, *, credentials_present: bool = False) -> None:
        self._lock = threading.RLock()
        self._stores: dict[str, TopicStore] = {}
        self._credentials_present = credentials_present
        self._messages_seen = 0
        self._last_message_at: datetime | None = None
        self._unknown_topics: set[str] = set()

    # -- ingest -------------------------------------------------------------

    def _store(self, topic: str) -> TopicStore:
        store = self._stores.get(topic)
        if store is None:
            store = TopicStore(topic)
            self._stores[topic] = store
        return store

    def ingest(self, topic: str, payload: Any) -> bool:
        """Apply one message. Safe to call from the recorder's thread."""
        if not isinstance(payload, dict):
            return False
        with self._lock:
            self._messages_seen += 1
            self._last_message_at = datetime.now(timezone.utc)
            if topic not in {
                TOPIC_DRIVERS, TOPIC_SESSIONS, TOPIC_POSITION, TOPIC_INTERVALS,
                TOPIC_LAPS, TOPIC_STINTS, TOPIC_PIT, TOPIC_LOCATION,
                TOPIC_CAR_DATA, TOPIC_RACE_CONTROL, TOPIC_TEAM_RADIO,
            }:
                # Record it anyway - an unexpected topic is data we should not
                # throw away - but log the name once so Friday's notes are easy.
                if topic not in self._unknown_topics:
                    self._unknown_topics.add(topic)
                    log.info("receiving an unlisted topic: %r", topic)
            return self._store(topic).upsert(payload)

    def on_raw_message(self, message: Any) -> None:
        """Subscriber callback for RawRecorder.add_subscriber."""
        self.ingest(message.topic, message.payload)

    # -- SessionDataSource --------------------------------------------------

    def get_drivers(self) -> list[Record]:
        with self._lock:
            return self._store(TOPIC_DRIVERS).all()

    def get_locations(self, since: datetime | None = None) -> list[Record]:
        with self._lock:
            return _filter_since(self._store(TOPIC_LOCATION).all(), since, "date")

    def get_positions(self, since: datetime | None = None) -> list[Record]:
        with self._lock:
            return _filter_since(self._store(TOPIC_POSITION).all(), since, "date")

    def get_intervals(self, since: datetime | None = None) -> list[Record]:
        with self._lock:
            return _filter_since(self._store(TOPIC_INTERVALS).all(), since, "date")

    def get_laps(self, since: datetime | None = None) -> list[Record]:
        with self._lock:
            return _filter_since(self._store(TOPIC_LAPS).all(), since, "date_start")

    def get_stints(self) -> list[Record]:
        with self._lock:
            return self._store(TOPIC_STINTS).all()

    def get_pits(self, since: datetime | None = None) -> list[Record]:
        with self._lock:
            return _filter_since(self._store(TOPIC_PIT).all(), since, "date")

    def get_team_radio(self, since: datetime | None = None) -> list[Record]:
        with self._lock:
            return _filter_since(self._store(TOPIC_TEAM_RADIO).all(), since, "date")

    def get_race_control(self, since: datetime | None = None) -> list[Record]:
        with self._lock:
            return _filter_since(self._store(TOPIC_RACE_CONTROL).all(), since, "date")

    def get_car_data(self, since: datetime | None = None) -> list[Record]:
        with self._lock:
            return _filter_since(self._store(TOPIC_CAR_DATA).all(), since, "date")

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
            )

    def _build_session(self) -> SessionInfo | None:
        records = self._store(TOPIC_SESSIONS).all()
        if not records:
            return None
        latest = records[-1]
        return SessionInfo(
            **{k: latest.get(k) for k in SessionInfo.model_fields if k in latest}
        )

    def _build_drivers(self) -> list[DriverState]:
        entries = self._store(TOPIC_DRIVERS).by_driver()
        positions = self._store(TOPIC_POSITION).by_driver()
        intervals = self._store(TOPIC_INTERVALS).by_driver()
        car_data = self._store(TOPIC_CAR_DATA).by_driver()

        laps_by_driver: dict[int, list[Record]] = {}
        for lap in self._store(TOPIC_LAPS).all():
            number = lap.get("driver_number")
            if isinstance(number, int):
                laps_by_driver.setdefault(number, []).append(lap)

        stints_by_driver: dict[int, list[Record]] = {}
        for stint in self._store(TOPIC_STINTS).all():
            number = stint.get("driver_number")
            if isinstance(number, int):
                stints_by_driver.setdefault(number, []).append(stint)

        pit_counts: dict[int, int] = {}
        latest_pit_lap: dict[int, int] = {}
        for pit in self._store(TOPIC_PIT).all():
            number = pit.get("driver_number")
            if isinstance(number, int):
                pit_counts[number] = pit_counts.get(number, 0) + 1
                lap = pit.get("lap_number")
                if isinstance(lap, int):
                    latest_pit_lap[number] = max(latest_pit_lap.get(number, 0), lap)

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
            | set(pit_counts)
        )

        states: list[DriverState] = []
        for number in numbers:
            entry = entries.get(number, {})
            position = positions.get(number, {})
            interval = intervals.get(number, {})
            car = car_data.get(number, {})

            laps = sorted(
                laps_by_driver.get(number, []),
                key=lambda lap: lap.get("lap_number") or 0,
            )
            latest_lap = laps[-1] if laps else {}
            completed = [
                lap.get("lap_duration")
                for lap in laps
                if isinstance(lap.get("lap_duration"), (int, float))
            ]

            # "Last lap" means the most recent *completed* lap, not the lap in
            # progress, whose lap_duration is still null.
            last_completed = next(
                (
                    lap
                    for lap in reversed(laps)
                    if isinstance(lap.get("lap_duration"), (int, float))
                ),
                {},
            )

            stints = sorted(
                stints_by_driver.get(number, []),
                key=lambda stint: stint.get("stint_number") or 0,
            )
            stint = stints[-1] if stints else {}
            current_lap = latest_lap.get("lap_number")

            states.append(
                DriverState(
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
                    # Derived, not reported: OpenF1 has no "car is in the pit
                    # lane right now" field. A pit record for the lap currently
                    # in progress is the best available signal, and it clears
                    # once the driver starts a new lap.
                    in_pit=(
                        current_lap is not None
                        and latest_pit_lap.get(number) == current_lap
                    ),
                    pit_count=pit_counts.get(number, 0),
                    aero_raw=car.get("drs"),
                    speed=car.get("speed"),
                    updated_at=interval.get("date") or position.get("date"),
                )
            )

        # Sort by position, with unclassified cars last but still shown.
        states.sort(key=lambda d: (d.position is None, d.position or 0, d.driver_number))

        # Interval to the car behind is just the following car's interval to
        # the car ahead - it is the same gap seen from the other side.
        for ahead, behind in zip(states, states[1:]):
            ahead.interval_behind = behind.interval_ahead

        return states

    def _build_race_control_messages(self) -> list[RaceControlMessage]:
        records = sorted(
            self._store(TOPIC_RACE_CONTROL).all(),
            key=lambda record: str(record.get("date") or ""),
        )
        recent = records[-RACE_CONTROL_LIMIT:]
        return [
            RaceControlMessage(
                **{k: record.get(k) for k in RaceControlMessage.model_fields if k in record}
            )
            for record in recent
        ]

    def _build_race_control_state(self) -> tuple[str | None, str | None, bool]:
        """Current track flag, session status, and the 2026 partial-aero badge."""
        records = sorted(
            self._store(TOPIC_RACE_CONTROL).all(),
            key=lambda record: str(record.get("date") or ""),
        )

        flag: str | None = None
        status: str | None = None
        partial_aero = False

        for record in records:
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

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "messages_seen": self._messages_seen,
                "last_message_at": (
                    self._last_message_at.isoformat(timespec="milliseconds")
                    if self._last_message_at
                    else None
                ),
                "topics": {
                    topic: {
                        "records": len(store),
                        "inserts": store.inserts,
                        "updates": store.updates,
                        "stale_drops": store.stale_drops,
                    }
                    for topic, store in sorted(self._stores.items())
                },
            }
