import type { DriverState, FeedInfo, SessionState } from '../types/sessionState'

/**
 * Runtime validation of a WebSocket frame before it touches the store.
 *
 * `JSON.parse(...) as SessionState` is a promise, not a check. The backend
 * validates its own inputs, but a frame can still be truncated by a proxy,
 * come from an older or newer backend, or be something else entirely on the
 * same port. Anything that would make a component throw mid-render is
 * rejected here with a reason, and the last good snapshot stays on screen.
 *
 * The checks are deliberately about *shape the UI relies on*, not a full
 * re-implementation of the pydantic models: every field a component reads
 * must be of a type that component can format.
 */

export type ValidationResult =
  | { ok: true; snapshot: SessionState }
  | { ok: false; reason: string }

const MODES = new Set(['live', 'replay', 'historical', 'demo', 'idle'])
const FEED_STATES = new Set(['offline', 'connecting', 'auth_failed', 'connected', 'live', 'stale'])

type Kind = 'number' | 'string' | 'boolean' | 'gap'

/** Every DriverState field the leaderboard reads, and what it may hold (null allowed). */
const DRIVER_FIELDS: Record<keyof Omit<DriverState, 'driver_number'>, Kind> = {
  name_acronym: 'string',
  full_name: 'string',
  broadcast_name: 'string',
  team_name: 'string',
  team_colour: 'string',
  position: 'number',
  gap_to_leader: 'gap',
  interval_ahead: 'gap',
  interval_behind: 'gap',
  lap_number: 'number',
  last_lap_duration: 'number',
  best_lap_duration: 'number',
  sector_1: 'number',
  sector_2: 'number',
  sector_3: 'number',
  is_pit_out_lap: 'boolean',
  compound: 'string',
  stint_number: 'number',
  tyre_age: 'number',
  in_pit: 'boolean',
  pit_count: 'number',
  aero_raw: 'number',
  speed: 'number',
  updated_at: 'string',
  x: 'number',
  y: 'number',
  location_at: 'string',
}

const DRIVER_DEFAULTS: Record<keyof Omit<DriverState, 'driver_number'>, unknown> = {
  name_acronym: null,
  full_name: null,
  broadcast_name: null,
  team_name: null,
  team_colour: null,
  position: null,
  gap_to_leader: null,
  interval_ahead: null,
  interval_behind: null,
  lap_number: null,
  last_lap_duration: null,
  best_lap_duration: null,
  sector_1: null,
  sector_2: null,
  sector_3: null,
  is_pit_out_lap: false,
  compound: null,
  stint_number: null,
  tyre_age: null,
  in_pit: false,
  pit_count: 0,
  aero_raw: null,
  speed: null,
  updated_at: null,
  x: null,
  y: null,
  location_at: null,
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function matches(kind: Kind, value: unknown): boolean {
  if (value === null || value === undefined) return true
  switch (kind) {
    case 'number':
      return typeof value === 'number' && Number.isFinite(value)
    case 'string':
      return typeof value === 'string'
    case 'boolean':
      return typeof value === 'boolean'
    case 'gap':
      return (typeof value === 'number' && Number.isFinite(value)) || typeof value === 'string'
  }
}

function validateDriver(value: unknown, index: number): { ok: true; driver: DriverState } | { ok: false; reason: string } {
  if (!isObject(value)) return { ok: false, reason: `drivers[${index}] is not an object` }
  const number = value.driver_number
  if (typeof number !== 'number' || !Number.isFinite(number)) {
    return { ok: false, reason: `drivers[${index}].driver_number is not a number` }
  }
  const driver: Record<string, unknown> = { driver_number: number }
  for (const [field, kind] of Object.entries(DRIVER_FIELDS) as [keyof typeof DRIVER_FIELDS, Kind][]) {
    const present = value[field]
    if (!matches(kind, present)) {
      return { ok: false, reason: `drivers[${index}].${field} is not a ${kind}` }
    }
    driver[field] = present === undefined ? DRIVER_DEFAULTS[field] : present
  }
  return { ok: true, driver: driver as unknown as DriverState }
}

function validateFeed(value: unknown): { ok: true; feed: FeedInfo | null } | { ok: false; reason: string } {
  if (value === null || value === undefined) return { ok: true, feed: null }
  if (!isObject(value)) return { ok: false, reason: 'feed is not an object' }
  if (typeof value.state !== 'string' || !FEED_STATES.has(value.state)) {
    return { ok: false, reason: `feed.state is not a known state` }
  }
  const checks: [string, Kind][] = [
    ['mqtt_connected', 'boolean'],
    ['authenticated', 'boolean'],
    ['last_message_at', 'string'],
    ['data_age_seconds', 'number'],
    ['stale_after_seconds', 'number'],
    ['recording_ok', 'boolean'],
    ['last_error', 'string'],
  ]
  for (const [field, kind] of checks) {
    if (!matches(kind, value[field])) return { ok: false, reason: `feed.${field} is not a ${kind}` }
  }
  return {
    ok: true,
    feed: {
      state: value.state as FeedInfo['state'],
      mqtt_connected: (value.mqtt_connected as boolean | undefined) ?? false,
      authenticated: (value.authenticated as boolean | undefined) ?? false,
      last_message_at: (value.last_message_at as string | null | undefined) ?? null,
      data_age_seconds: (value.data_age_seconds as number | null | undefined) ?? null,
      stale_after_seconds: (value.stale_after_seconds as number | undefined) ?? 15,
      recording_ok: (value.recording_ok as boolean | undefined) ?? true,
      last_error: (value.last_error as string | null | undefined) ?? null,
    },
  }
}

export function validateSnapshot(input: unknown): ValidationResult {
  if (!isObject(input)) return { ok: false, reason: 'frame is not an object' }
  if (input.type !== 'snapshot') return { ok: false, reason: 'type is not "snapshot"' }
  if (typeof input.phase !== 'number' || !Number.isFinite(input.phase)) {
    return { ok: false, reason: 'phase is not a number' }
  }
  if (typeof input.mode !== 'string' || !MODES.has(input.mode)) {
    return { ok: false, reason: 'mode is not a known mode' }
  }
  if (typeof input.server_time !== 'string') return { ok: false, reason: 'server_time is not a string' }
  if (input.credentials_present !== undefined && typeof input.credentials_present !== 'boolean') {
    return { ok: false, reason: 'credentials_present is not a boolean' }
  }
  if (!Array.isArray(input.drivers)) return { ok: false, reason: 'drivers is not an array' }
  if (input.session !== undefined && input.session !== null && !isObject(input.session)) {
    return { ok: false, reason: 'session is not an object' }
  }
  if (input.race_control !== undefined && !Array.isArray(input.race_control)) {
    return { ok: false, reason: 'race_control is not an array' }
  }
  if (!matches('string', input.track_flag)) return { ok: false, reason: 'track_flag is not a string' }
  if (!matches('string', input.session_status)) return { ok: false, reason: 'session_status is not a string' }
  if (input.partial_aero !== undefined && typeof input.partial_aero !== 'boolean') {
    return { ok: false, reason: 'partial_aero is not a boolean' }
  }
  if (!matches('number', input.session_best_lap)) return { ok: false, reason: 'session_best_lap is not a number' }
  if (input.recorder !== undefined && input.recorder !== null && !isObject(input.recorder)) {
    return { ok: false, reason: 'recorder is not an object' }
  }
  if (input.adapter !== undefined && input.adapter !== null && !isObject(input.adapter)) {
    return { ok: false, reason: 'adapter is not an object' }
  }
  if (input.replay !== undefined && input.replay !== null && !isObject(input.replay)) {
    return { ok: false, reason: 'replay is not an object' }
  }
  if (input.degraded !== undefined && typeof input.degraded !== 'boolean') {
    return { ok: false, reason: 'degraded is not a boolean' }
  }
  if (!matches('string', input.degraded_reason)) return { ok: false, reason: 'degraded_reason is not a string' }

  const feed = validateFeed(input.feed)
  if (!feed.ok) return feed

  const drivers: DriverState[] = []
  for (let index = 0; index < input.drivers.length; index += 1) {
    const result = validateDriver(input.drivers[index], index)
    if (!result.ok) return result
    drivers.push(result.driver)
  }

  const snapshot: SessionState = {
    type: 'snapshot',
    phase: input.phase,
    mode: input.mode as SessionState['mode'],
    server_time: input.server_time,
    credentials_present: (input.credentials_present as boolean | undefined) ?? false,
    session: (input.session as SessionState['session'] | undefined) ?? null,
    drivers,
    track_flag: (input.track_flag as string | null | undefined) ?? null,
    session_status: (input.session_status as string | null | undefined) ?? null,
    partial_aero: (input.partial_aero as boolean | undefined) ?? false,
    session_best_lap: (input.session_best_lap as number | null | undefined) ?? null,
    race_control: (input.race_control as SessionState['race_control'] | undefined) ?? [],
    recorder: (input.recorder as SessionState['recorder'] | undefined) ?? null,
    feed: feed.feed,
    adapter: (input.adapter as SessionState['adapter'] | undefined) ?? null,
    replay: (input.replay as SessionState['replay'] | undefined) ?? null,
    degraded: (input.degraded as boolean | undefined) ?? false,
    degraded_reason: (input.degraded_reason as string | null | undefined) ?? null,
  }
  return { ok: true, snapshot }
}

/** Parse and validate one WebSocket text frame. Never throws. */
export function parseSnapshotFrame(text: string): ValidationResult {
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    return { ok: false, reason: 'frame is not valid JSON' }
  }
  return validateSnapshot(parsed)
}
