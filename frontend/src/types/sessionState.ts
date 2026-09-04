/**
 * GENERATED FILE - DO NOT EDIT BY HAND.
 *
 * Generated from backend/app/models.py by backend/scripts/generate_ts_types.py.
 * Regenerate with:  make types
 *
 * A backend test fails if this file drifts from the pydantic models, so the two
 * sides of the WebSocket cannot disagree about the shape of a snapshot.
 */


/** What the live adapter did with the stream: scoping, validation, drops. */
export interface AdapterInfo {
  active_session_key: number | null
  sessions_retained: number[]
  messages_seen: number
  /** Records rejected outright: not a dict, or missing an identity field. */
  quarantined_records: number
  /** Fields dropped from otherwise valid records for having the wrong type. */
  malformed_fields: number
  /** Messages for a session older than the active one. Never shown. */
  late_session_messages: number
  session_switches: number
  driver_build_errors: number
  /**
   * Rows fetched over REST by `backfill.py` rather than received over MQTT.
   *
   * Counted apart from `messages_seen` so that stays a count of what actually
   * came off the wire. A mid-session connect misses the one-shot `v1/sessions`
   * and `v1/drivers` announcements, and this is how they are recovered.
   */
  backfilled_records: number
}

/** Everything the leaderboard needs about one car, already merged. */
export interface DriverState {
  driver_number: number
  name_acronym: string | null
  full_name: string | null
  broadcast_name: string | null
  team_name: string | null
  /** Bare hex, no "#". Falls back to teams.ts when absent. */
  team_colour: string | null
  position: number | null
  gap_to_leader: number | string | null
  interval_ahead: number | string | null
  /** Derived: the interval reported by the car directly behind this one. */
  interval_behind: number | string | null
  lap_number: number | null
  last_lap_duration: number | null
  best_lap_duration: number | null
  sector_1: number | null
  sector_2: number | null
  sector_3: number | null
  is_pit_out_lap: boolean
  compound: string | null
  stint_number: number | null
  /** current lap - stint lap_start + tyre_age_at_start. */
  tyre_age: number | null
  /**
   * Derived, not reported: true when this driver has a pit record for the
   * lap currently in progress. OpenF1 exposes no live "in the pit lane" flag.
   */
  in_pit: boolean
  pit_count: number
  /**
   * The legacy `drs` integer from car_data, stored WITHOUT interpretation.
   *
   * 2026 removed DRS. Whether this field now carries the Active Aero state,
   * Overtake Mode, or nothing at all is unknown until the Monza FP1 recording is
   * analysed. It is deliberately named `aero_raw` so nothing downstream is
   * tempted to treat it as a DRS flag, and the AERO/OT columns stay absent until
   * `aero.py` exists.
   */
  aero_raw: number | null
  speed: number | null
  updated_at: string | null
  /** Car position from `v1/location`, in OpenF1's circuit coordinate frame. */
  x: number | null
  y: number | null
  /** `date` of the location sample the x/y came from. */
  location_at: string | null
}

/**
 * Separates "the socket is open" from "data is actually arriving".
 *
 * A LIVE badge must mean all of: browser socket open, MQTT connected,
 * authenticated, and a message received recently. Any one of those failing
 * is a different problem with a different fix, so each is reported.
 */
export interface FeedInfo {
  state: 'offline' | 'connecting' | 'auth_failed' | 'connected' | 'live' | 'stale'
  mqtt_connected: boolean
  authenticated: boolean
  last_message_at: string | null
  /** Seconds since the recorder last received any message. Null before the first. */
  data_age_seconds: number | null
  stale_after_seconds: number
  recording_ok: boolean
  last_error: string | null
}

export interface RaceControlMessage {
  date: string | null
  category: string | null
  flag: string | null
  scope: string | null
  sector: number | null
  lap_number: number | null
  driver_number: number | null
  message: string | null
}

/**
 * Recorder health, so the UI can show whether data is actually arriving
 * and whether the on-disk capture can still be trusted.
 */
export interface RecorderInfo {
  connected: boolean
  messages_recorded: number
  last_message_at: string | null
  topics: Record<string, number>
  token_expires_at: string | null
  last_error: string | null
  /** False while writes to the recordings directory are failing. */
  recording_ok: boolean
  write_failures: number
  /**
   * Messages recorded to disk but dropped from the in-process fan-out queue
   * because the live adapter could not keep up. The recording is unaffected.
   */
  fanout_dropped: number
  /** Estimated from gaps in OpenF1's `_id` sequence. Heuristic: see README. */
  messages_possibly_lost: number
  /**
   * True once anything may have been lost: a write failure, a disconnect
   * after data had started flowing, or an `_id` gap. Never reset.
   */
  may_be_incomplete: boolean
  disk_free_bytes: number | null
  disk_low: boolean
}

/** Transport state of a recording being replayed. Present only in replay mode. */
export interface ReplayInfo {
  session_key: number
  state: 'loaded' | 'playing' | 'paused' | 'seeking' | 'finished'
  /** Playback rate relative to the recorded pacing (1 = as recorded). */
  speed: number
  /** `received_at` of the last message applied. */
  position: string | null
  start: string | null
  end: string | null
  /** 0..1 through the recording. */
  progress: number
  messages_replayed: number
  /** Lines of the recording that could not be parsed. */
  skipped_lines: number
  ingest_errors: number
}

/** Identity of the session being shown. From the v1/sessions topic. */
export interface SessionInfo {
  session_key: number | null
  meeting_key: number | null
  circuit_key: number | null
  session_name: string | null
  session_type: string | null
  circuit_short_name: string | null
  country_name: string | null
  location: string | null
  year: number | null
  date_start: string | null
  date_end: string | null
  /** Formatted "02:00:00", not a number of hours. Parse before using. */
  gmt_offset: string | null
}

/** The complete snapshot pushed over the WebSocket. */
export interface SessionState {
  type: 'snapshot'
  phase: number
  mode: 'live' | 'replay' | 'historical' | 'demo' | 'idle'
  server_time: string
  credentials_present: boolean
  session: SessionInfo | null
  drivers: DriverState[]
  track_flag: string | null
  session_status: string | null
  /** 2026: race control can enable front-Straight/rear-Corner in the wet. */
  partial_aero: boolean
  /** Drives the purple timing colour without the frontend rescanning rows. */
  session_best_lap: number | null
  race_control: RaceControlMessage[]
  recorder: RecorderInfo | null
  feed: FeedInfo | null
  adapter: AdapterInfo | null
  replay: ReplayInfo | null
  /**
   * True when this frame is a re-send of the last good snapshot (or an empty
   * one) because building a fresh snapshot failed. See `degraded_reason`.
   */
  degraded: boolean
  degraded_reason: string | null
}
