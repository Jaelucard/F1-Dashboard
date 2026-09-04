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
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return NO_DATA
  if (seconds < 0) return NO_DATA

  // Round to whole milliseconds *before* splitting into minutes. Splitting
  // first and rounding the remainder turns 59.9996 into "60.000" and 119.9996
  // into "1:60.000", because the remainder rounds up past the minute.
  const totalMs = Math.round(seconds * 1000)
  const minutes = Math.floor(totalMs / 60_000)
  const restMs = totalMs - minutes * 60_000
  const restText = (restMs / 1000).toFixed(3)
  return minutes > 0 ? `${minutes}:${restText.padStart(6, '0')}` : restText
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

export const SECTOR_TOLERANCE_SECONDS = 0.0005

/**
 * Which colour a sector time gets. Same purple/green/yellow/none rule as
 * `lapTimeColour`, but with a tolerance rather than exact equality.
 *
 * Sector values are computed on the backend (`sector_1/2/3` from whichever
 * lap is currently selected, `best_sector_n`/`session_best_sectors` as a
 * running minimum recomputed every snapshot) rather than carried forward
 * unchanged from one OpenF1 message, so float drift between "this value" and
 * "the minimum it should match" is possible in a way it is not for lap times.
 */
export function sectorColour(
  value: number | null | undefined,
  personalBest: number | null | undefined,
  sessionBest: number | null | undefined,
): TimingColour {
  if (value === null || value === undefined || !Number.isFinite(value)) return 'none'
  if (
    sessionBest !== null &&
    sessionBest !== undefined &&
    Math.abs(value - sessionBest) <= SECTOR_TOLERANCE_SECONDS
  ) {
    return 'best'
  }
  if (
    personalBest !== null &&
    personalBest !== undefined &&
    Math.abs(value - personalBest) <= SECTOR_TOLERANCE_SECONDS
  ) {
    return 'personal'
  }
  return 'slower'
}

/**
 * Wall-clock time of one race control message, HH:MM:SS in UTC - the frame
 * OpenF1 timestamps in, and what the "since" column needs to stay meaningful
 * however the viewer's own timezone is set.
 */
export function formatUtcClock(iso: string | null | undefined): string {
  if (!iso) return NO_DATA
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return NO_DATA
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}`
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
