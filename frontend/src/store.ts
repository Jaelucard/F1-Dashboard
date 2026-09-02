import { create } from 'zustand'
import type { SessionState } from './types/sessionState'
import type { ConnectionStatus } from './lib/types'
import { parseSnapshotFrame } from './lib/validateSnapshot'

/**
 * The single store. One SessionState in, components subscribe to slices.
 *
 * Snapshots replace wholesale rather than merge: the backend already sends a
 * complete, self-consistent state every second, so merging would only risk
 * mixing fields from two different instants.
 *
 * Frames are validated before they are applied. An invalid frame is counted
 * and its reason kept, but it never overwrites a valid snapshot and never
 * advances `lastMessageAt` - a stream of garbage must look stale, not alive.
 *
 * `lastMessageAt` is stored as epoch ms rather than a Date so that a snapshot
 * carrying identical data does not create a new object identity and re-render
 * every subscriber.
 */
interface Store {
  snapshot: SessionState | null
  status: ConnectionStatus
  lastMessageAt: number | null
  reconnectAttempts: number
  invalidFrames: number
  lastInvalidReason: string | null

  applySnapshot: (snapshot: SessionState) => void
  /** Validate and apply one raw WebSocket frame. Returns whether it was applied. */
  applyFrame: (text: string) => boolean
  setStatus: (status: ConnectionStatus) => void
  setReconnectAttempts: (attempts: number) => void
}

export const useStore = create<Store>((set, get) => ({
  snapshot: null,
  status: 'connecting',
  lastMessageAt: null,
  reconnectAttempts: 0,
  invalidFrames: 0,
  lastInvalidReason: null,

  applySnapshot: (snapshot) => set({ snapshot, lastMessageAt: Date.now() }),
  applyFrame: (text) => {
    const result = parseSnapshotFrame(text)
    if (!result.ok) {
      const count = get().invalidFrames + 1
      if (count === 1 || count % 100 === 0) {
        console.warn(`discarded invalid snapshot frame (${count} so far): ${result.reason}`)
      }
      set({ invalidFrames: count, lastInvalidReason: result.reason })
      return false
    }
    set({ snapshot: result.snapshot, lastMessageAt: Date.now() })
    return true
  },
  setStatus: (status) => set({ status }),
  setReconnectAttempts: (reconnectAttempts) => set({ reconnectAttempts }),
}))

// Selectors, so components subscribe to the narrowest slice they need.
export const selectDrivers = (state: Store) => state.snapshot?.drivers ?? EMPTY_DRIVERS
export const selectSession = (state: Store) => state.snapshot?.session ?? null
export const selectSessionBest = (state: Store) => state.snapshot?.session_best_lap ?? null

// A stable empty array: returning a fresh [] from a selector would make every
// subscriber re-render on every snapshot.
const EMPTY_DRIVERS: SessionState['drivers'] = []
