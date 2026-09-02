"""The one normalised shape the browser ever sees.

Every mode - live, replay, historical - produces a ``SessionState`` and nothing
else, so the frontend cannot tell them apart. The TypeScript equivalent in
``frontend/src/types/sessionState.ts`` is *generated* from these models by
``scripts/generate_ts_types.py``; a test fails if the checked-in file drifts.

Field types were confirmed against real OpenF1 responses rather than the docs:

* ``gap_to_leader`` and ``interval`` are a **float, the string "+1 LAP", or
  null**. The lapped-car case really does arrive as a string, so the model has
  to admit both and the formatter has to handle both.
* ``compound`` arrives uppercase ("MEDIUM", "SOFT").
* ``team_colour`` is a bare hex string with no leading "#".
* ``lap_duration`` and the sector durations are floats in seconds.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# OpenF1 sends a number of seconds, or "+1 LAP" for a lapped car, or null.
GapValue = float | str | None

Mode = Literal["live", "replay", "historical", "demo", "idle"]
"""How the data was obtained.

"demo" is deliberately its own value rather than being dressed up as "live":
a synthetic grid must never be able to display a LIVE badge, or you could sit
through a session believing you are recording it.
"""


class Base(BaseModel):
    """Shared config: attribute docstrings become JSON Schema descriptions.

    That means a comment written once next to the Python field ends up as JSDoc
    on the generated TypeScript, so a frontend developer reading DriverState
    sees the warning about aero_raw without opening the backend.
    """

    model_config = ConfigDict(use_attribute_docstrings=True)


class SessionInfo(Base):
    """Identity of the session being shown. From the v1/sessions topic."""

    session_key: int | None = None
    meeting_key: int | None = None
    circuit_key: int | None = None
    session_name: str | None = None
    session_type: str | None = None
    circuit_short_name: str | None = None
    country_name: str | None = None
    location: str | None = None
    year: int | None = None
    date_start: str | None = None
    date_end: str | None = None
    gmt_offset: str | None = None
    """Formatted "02:00:00", not a number of hours. Parse before using."""


class RaceControlMessage(Base):
    date: str | None = None
    category: str | None = None
    flag: str | None = None
    scope: str | None = None
    sector: int | None = None
    lap_number: int | None = None
    driver_number: int | None = None
    message: str | None = None


class DriverState(Base):
    """Everything the leaderboard needs about one car, already merged."""

    driver_number: int
    name_acronym: str | None = None
    full_name: str | None = None
    broadcast_name: str | None = None
    team_name: str | None = None
    team_colour: str | None = None
    """Bare hex, no "#". Falls back to teams.ts when absent."""

    position: int | None = None
    gap_to_leader: GapValue = None
    interval_ahead: GapValue = None
    interval_behind: GapValue = None
    """Derived: the interval reported by the car directly behind this one."""

    lap_number: int | None = None
    last_lap_duration: float | None = None
    best_lap_duration: float | None = None
    sector_1: float | None = None
    sector_2: float | None = None
    sector_3: float | None = None
    is_pit_out_lap: bool = False

    compound: str | None = None
    stint_number: int | None = None
    tyre_age: int | None = None
    """current lap - stint lap_start + tyre_age_at_start."""

    in_pit: bool = False
    """Derived, not reported: true when this driver has a pit record for the
    lap currently in progress. OpenF1 exposes no live "in the pit lane" flag."""

    pit_count: int = 0

    aero_raw: int | None = None
    """The legacy ``drs`` integer from car_data, stored WITHOUT interpretation.

    2026 removed DRS. Whether this field now carries the Active Aero state,
    Overtake Mode, or nothing at all is unknown until the Monza FP1 recording is
    analysed. It is deliberately named ``aero_raw`` so nothing downstream is
    tempted to treat it as a DRS flag, and the AERO/OT columns stay absent until
    ``aero.py`` exists.
    """

    speed: int | None = None
    updated_at: str | None = None


class RecorderInfo(Base):
    """Recorder health, so the UI can show whether data is actually arriving
    and whether the on-disk capture can still be trusted."""

    connected: bool = False
    messages_recorded: int = 0
    last_message_at: str | None = None
    topics: dict[str, int] = Field(default_factory=dict)
    token_expires_at: str | None = None
    last_error: str | None = None

    recording_ok: bool = True
    """False while writes to the recordings directory are failing."""
    write_failures: int = 0
    fanout_dropped: int = 0
    """Messages recorded to disk but dropped from the in-process fan-out queue
    because the live adapter could not keep up. The recording is unaffected."""
    messages_possibly_lost: int = 0
    """Estimated from gaps in OpenF1's ``_id`` sequence. Heuristic: see README."""
    may_be_incomplete: bool = False
    """True once anything may have been lost: a write failure, a disconnect
    after data had started flowing, or an ``_id`` gap. Never reset."""
    disk_free_bytes: int | None = None
    disk_low: bool = False


FeedState = Literal["offline", "connecting", "auth_failed", "connected", "live", "stale"]
"""Upstream feed state, distinct from whether this backend process is up.

offline      no recorder running (LIVE_MODE off, or it failed to start)
connecting   recorder running, MQTT not (yet) connected
auth_failed  the broker or token endpoint rejected the credentials
connected    MQTT up but no message has ever arrived (quiet outside a session)
live         MQTT up and a message arrived within ``stale_after_seconds``
stale        data had been arriving but has stopped, or MQTT dropped after data
"""


class FeedInfo(Base):
    """Separates "the socket is open" from "data is actually arriving".

    A LIVE badge must mean all of: browser socket open, MQTT connected,
    authenticated, and a message received recently. Any one of those failing
    is a different problem with a different fix, so each is reported.
    """

    state: FeedState
    mqtt_connected: bool = False
    authenticated: bool = False
    last_message_at: str | None = None
    data_age_seconds: float | None = None
    """Seconds since the recorder last received any message. Null before the first."""
    stale_after_seconds: float
    recording_ok: bool = True
    last_error: str | None = None


class AdapterInfo(Base):
    """What the live adapter did with the stream: scoping, validation, drops."""

    active_session_key: int | None = None
    sessions_retained: list[int] = Field(default_factory=list)
    messages_seen: int = 0
    quarantined_records: int = 0
    """Records rejected outright: not a dict, or missing an identity field."""
    malformed_fields: int = 0
    """Fields dropped from otherwise valid records for having the wrong type."""
    late_session_messages: int = 0
    """Messages for a session older than the active one. Never shown."""
    session_switches: int = 0
    driver_build_errors: int = 0


class SessionState(Base):
    """The complete snapshot pushed over the WebSocket."""

    type: Literal["snapshot"] = "snapshot"
    phase: int
    mode: Mode
    server_time: str
    credentials_present: bool = False

    session: SessionInfo | None = None
    drivers: list[DriverState] = Field(default_factory=list)

    track_flag: str | None = None
    session_status: str | None = None
    partial_aero: bool = False
    """2026: race control can enable front-Straight/rear-Corner in the wet."""

    session_best_lap: float | None = None
    """Drives the purple timing colour without the frontend rescanning rows."""

    race_control: list[RaceControlMessage] = Field(default_factory=list)
    recorder: RecorderInfo | None = None
    feed: FeedInfo | None = None
    adapter: AdapterInfo | None = None

    degraded: bool = False
    """True when this frame is a re-send of the last good snapshot (or an empty
    one) because building a fresh snapshot failed. See ``degraded_reason``."""
    degraded_reason: str | None = None
