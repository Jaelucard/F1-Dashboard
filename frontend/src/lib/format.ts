/**
 * Timing formatters.
 *
 * OpenF1's gap fields are the awkward part. Confirmed against real 2025 race
 * data, `gap_to_leader` and `interval` arrive as:
 *   - a number of seconds (0.423)
 *   - the string "+1 LAP" for a lapped car
 *   - null, while the value is unknown (the first laps, or after a stop)
 *
 * So every gap formatter has to accept `number | string | null`. Strings are
 * passed through as-is rather than parsed: OpenF1 has already formatted them,
 * and guessing at other string forms would invent data.
 */

export type GapValue = number | string | null

/** Placeholder for "no data". Kept as one constant so columns line up. */
export const NO_DATA = '—'

/**
 * A lap or sector time as M:SS.mmm, or SS.mmm under a minute.
 *
 * Sector times are usually under a minute and read better without a leading
 * "0:", but a slow lap behind a safety car can exceed one, so both are handled.
 */
export function formatLapTime(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return NO_DATA
  if (seconds < 0) return NO_DATA

  const minutes = Math.floor(seconds / 60)
  const rest = seconds - minutes * 60
  const restText = rest.toFixed(3).padStart(6, '0')
  return minutes > 0 ? `${minutes}:${restText}` : rest.toFixed(3)
}

/**
 * Gap to the leader: "LEADER" for P1, "+1.234", or "+1 LAP" passed through.
 *
 * `isLeader` is passed in rather than inferred from a zero gap, because the
 * leader's gap legitimately reads 0.000 and so does a car that has just been
 * caught at the line.
 */
export function formatGap(value: GapValue, isLeader = false): string {
  if (isLeader) return 'LEADER'
  if (value === null || value === undefined) return NO_DATA
  if (typeof value === 'string') {
    const text = value.trim()
    if (!text) return NO_DATA
    // Already formatted by OpenF1, e.g. "+1 LAP".
    return text.startsWith('+') || text.startsWith('-') ? text : `+${text}`
  }
  if (!Number.isFinite(value)) return NO_DATA
  return `+${value.toFixed(3)}`
}

/** Interval to the car ahead. Same rules as the gap, minus the LEADER case. */
export function formatInterval(value: GapValue): string {
  if (value === null || value === undefined) return NO_DATA
  if (typeof value === 'string') {
    const text = value.trim()
    if (!text) return NO_DATA
    return text.startsWith('+') || text.startsWith('-') ? text : `+${text}`
  }
  if (!Number.isFinite(value)) return NO_DATA
  return `+${value.toFixed(3)}`
}

/** Tyre age in laps, e.g. "12L". A brand new set reads "0L", not "—". */
export function formatTyreAge(age: number | null | undefined): string {
  if (age === null || age === undefined || !Number.isFinite(age)) return NO_DATA
  return `${Math.max(0, Math.trunc(age))}L`
}

/** Seconds since the last frame, for the data-age counter. */
export function formatDataAge(seconds: number | null): string {
  if (seconds === null) return NO_DATA
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m${String(seconds % 60).padStart(2, '0')}s`
}

/**
 * Which colour a lap time gets.
 *
 * purple  session best
 * green   this driver's own best
 * yellow  slower than their own best
 * white   no data
 */
export type TimingColour = 'best' | 'personal' | 'slower' | 'none'

export function lapTimeColour(
  value: number | null | undefined,
  personalBest: number | null | undefined,
  sessionBest: number | null | undefined,
): TimingColour {
  if (value === null || value === undefined || !Number.isFinite(value)) return 'none'
  // Float equality is safe here: these are the same numbers that came off the
  // wire, compared against a minimum taken from that same set, not recomputed.
  if (sessionBest !== null && sessionBest !== undefined && value === sessionBest) return 'best'
  if (personalBest !== null && personalBest !== undefined && value === personalBest) {
    return 'personal'
  }
  return 'slower'
}

/** Session clock: elapsed time since the session started. */
export function formatSessionClock(startIso: string | null, now: Date = new Date()): string {
  if (!startIso) return NO_DATA
  const start = new Date(startIso)
  if (Number.isNaN(start.getTime())) return NO_DATA

  const totalSeconds = Math.floor((now.getTime() - start.getTime()) / 1000)
  const sign = totalSeconds < 0 ? '-' : ''
  const absolute = Math.abs(totalSeconds)
  const hours = Math.floor(absolute / 3600)
  const minutes = Math.floor((absolute % 3600) / 60)
  const seconds = absolute % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${sign}${pad(hours)}:${pad(minutes)}:${pad(seconds)}`
}
