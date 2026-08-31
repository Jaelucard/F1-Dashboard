/**
 * GENERATED FILE - DO NOT EDIT BY HAND.
 *
 * Generated from backend/app/models.py by backend/scripts/generate_ts_types.py.
 * Regenerate with:  make types
 *
 * A backend test fails if this file drifts from the pydantic models, so the two
 * sides of the WebSocket cannot disagree about the shape of a snapshot.
 */


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

/** Feed health, so the UI can show whether data is actually arriving. */
export interface RecorderInfo {
  connected: boolean
  messages_recorded: number
  last_message_at: string | null
  topics: Record<string, number>
  token_expires_at: string | null
  last_error: string | null
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
}
