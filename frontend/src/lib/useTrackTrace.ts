import { useEffect, useState, useSyncExternalStore } from 'react'
import { useStore } from '../store'
import type { DriverState } from '../types/sessionState'

/**
 * Fallback outline for a circuit with no bundled JSON: every car position
 * seen so far, so the track draws itself as the cars lap.
 *
 * A point is appended only when a driver's `location_at` changes, so the
 * once-a-second snapshot of a stationary car does not pile up. The ring is
 * capped, and reset when the session changes.
 *
 * The accumulator is an external store fed from the snapshot store, read
 * through `useSyncExternalStore`, so no component state is set inside an
 * effect.
 */

export const MAX_TRACE_POINTS = 4000

export type Point = [number, number]

export class TraceAccumulator {
  private points: Point[] = []
  private seen = new Map<number, string>()
  private sessionKey: number | null = null
  private listeners = new Set<() => void>()

  observe(drivers: DriverState[], sessionKey: number | null): void {
    let next = this.points
    if (this.sessionKey !== sessionKey) {
      this.sessionKey = sessionKey
      this.seen = new Map()
      next = []
    }
    const fresh: Point[] = []
    for (const driver of drivers) {
      if (driver.x == null || driver.y == null) continue
      const stamp = driver.location_at ?? `${driver.x},${driver.y}`
      if (this.seen.get(driver.driver_number) === stamp) continue
      this.seen.set(driver.driver_number, stamp)
      fresh.push([driver.x, driver.y])
    }
    if (fresh.length > 0) {
      next = next.concat(fresh)
      if (next.length > MAX_TRACE_POINTS) next = next.slice(next.length - MAX_TRACE_POINTS)
    }
    if (next !== this.points) {
      this.points = next
      for (const listener of this.listeners) listener()
    }
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  getSnapshot = (): Point[] => this.points
}

export function useTrackTrace(): Point[] {
  const [accumulator] = useState(() => new TraceAccumulator())

  useEffect(() => {
    const feed = (snapshot: ReturnType<typeof useStore.getState>['snapshot']) =>
      accumulator.observe(snapshot?.drivers ?? [], snapshot?.session?.session_key ?? null)
    feed(useStore.getState().snapshot)
    return useStore.subscribe((state) => feed(state.snapshot))
  }, [accumulator])

  return useSyncExternalStore(accumulator.subscribe, accumulator.getSnapshot)
}
