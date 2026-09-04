import type { SessionState } from '../types/sessionState'
import type { ConnectionStatus } from './types'

/**
 * Colours for OpenF1's track-flag values. Shared between the small chip in
 * StatusStrip and the large flag in FlagPanel, so the two can never disagree
 * about what a colour means.
 */
export const FLAG_STYLES: Record<string, string> = {
  YELLOW: 'bg-timing-slower text-black',
  'DOUBLE YELLOW': 'bg-timing-slower text-black',
  RED: 'bg-f1-red text-white',
  GREEN: 'bg-timing-personal text-black',
  BLUE: 'bg-[#3B82F6] text-white',
  CHEQUERED: 'bg-white text-black',
  'BLACK AND WHITE': 'bg-white text-black',
}

/**
 * What the badge in the status strip says, derived from three separate facts:
 *
 *   1. the browser's own WebSocket (`status`, and how long since a frame)
 *   2. what the backend says it is doing (`snapshot.mode`)
 *   3. what the backend says about upstream (`snapshot.feed.state`)
 *
 * LIVE requires all three to be good. Anything less is named for what is
 * actually wrong, because on a race Friday "LIVE" on a dead feed is the
 * single most expensive lie the UI can tell.
 */

export type BadgeTone = 'live' | 'demo' | 'warn' | 'bad' | 'idle'

export interface Badge {
  label: string
  tone: BadgeTone
  pulse: boolean
}

/** The backend pushes once a second; several missed pushes means it is gone. */
export const SOCKET_STALE_SECONDS = 5

export function feedBadge({
  status,
  snapshot,
  dataAgeSeconds,
}: {
  status: ConnectionStatus
  snapshot: SessionState | null
  dataAgeSeconds: number | null
}): Badge {
  if (status === 'closed') return { label: 'OFFLINE', tone: 'bad', pulse: false }
  if (snapshot === null || status !== 'open') return { label: 'CONNECTING', tone: 'warn', pulse: false }
  if (dataAgeSeconds !== null && dataAgeSeconds >= SOCKET_STALE_SECONDS) {
    return { label: 'STALE', tone: 'bad', pulse: false }
  }
  if (snapshot.degraded) return { label: 'DEGRADED', tone: 'warn', pulse: false }

  switch (snapshot.mode) {
    case 'demo':
      return { label: 'DEMO', tone: 'demo', pulse: false }
    case 'idle':
      return { label: 'IDLE', tone: 'idle', pulse: false }
    case 'replay':
    case 'historical':
      return { label: snapshot.mode.toUpperCase(), tone: 'idle', pulse: false }
    case 'live':
      break
  }

  switch (snapshot.feed?.state) {
    case 'live':
      return { label: 'LIVE', tone: 'live', pulse: true }
    case 'stale':
      return { label: 'STALE', tone: 'bad', pulse: false }
    case 'connected':
      return { label: 'WAITING', tone: 'warn', pulse: false }
    case 'connecting':
      return { label: 'CONNECTING', tone: 'warn', pulse: false }
    case 'auth_failed':
      return { label: 'AUTH FAILED', tone: 'bad', pulse: false }
    case 'offline':
      return { label: 'OFFLINE', tone: 'bad', pulse: false }
    default:
      // Live mode with no feed block: an older backend, or something missing.
      return { label: 'NO FEED', tone: 'bad', pulse: false }
  }
}
