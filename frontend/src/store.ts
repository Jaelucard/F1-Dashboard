import { create } from 'zustand'
import type { SessionState } from './types/sessionState'
import type { ConnectionStatus } from './lib/types'

/**
 * The single store. One SessionState in, components subscribe to slices.
 *
 * Snapshots replace wholesale rather than merge: the backend already sends a
 * complete, self-consistent state every second, so merging would only risk
 * mixing fields from two different instants.
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

  applySnapshot: (snapshot: SessionState) => void
  setStatus: (status: ConnectionStatus) => void
  setReconnectAttempts: (attempts: number) => void
}

export const useStore = create<Store>((set) => ({
  snapshot: null,
  status: 'connecting',
  lastMessageAt: null,
  reconnectAttempts: 0,

  applySnapshot: (snapshot) => set({ snapshot, lastMessageAt: Date.now() }),
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
