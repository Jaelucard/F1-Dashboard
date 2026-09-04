import { useEffect, useState, useSyncExternalStore } from 'react'
import { useStore } from '../store'
import type { DriverState } from '../types/sessionState'

/**
 * Fallback outline for a circuit with no bundled JSON: the path each car has
 * taken, so the track draws itself as the cars lap.
 *
 * One trace per driver, never a single shared list. A shared list interleaves
 * the cars - snapshot N holds car 1, then car 16, then car 44, all in different
 * places - so joining it into one polyline draws a starburst between cars
 * rather than a circuit.
 *
 * A point is appended only when a driver's `location_at` changes, so the
 * once-a-second snapshot of a stationary car does not pile up. Each trace is
 * capped, and all of them are dropped when the session changes.
 *
 * The accumulator is an external store fed from the snapshot store, read
 * through `useSyncExternalStore`, so no component state is set inside an
 * effect.
 */

export const MAX_TRACE_POINTS = 600
/** Per driver. At one snapshot a second that is several laps of any circuit. */

export type Point = [number, number]

export interface Trace {
  driver_number: number
  points: Point[]
}

export class TraceAccumulator {
  private traces = new Map<number, Point[]>()
  private snapshot: Trace[] = []
  private seen = new Map<number, string>()
  private sessionKey: number | null = null
  private listeners = new Set<() => void>()

  observe(drivers: DriverState[], sessionKey: number | null): void {
    let changed = false
    if (this.sessionKey !== sessionKey) {
      this.sessionKey = sessionKey
      this.seen = new Map()
      changed = this.traces.size > 0
      this.traces = new Map()
    }
    for (const driver of drivers) {
      if (driver.x == null || driver.y == null) continue
      const stamp = driver.location_at ?? `${driver.x},${driver.y}`
      if (this.seen.get(driver.driver_number) === stamp) continue
      this.seen.set(driver.driver_number, stamp)
      const previous = this.traces.get(driver.driver_number) ?? []
      const next = previous.concat([[driver.x, driver.y]])
      this.traces.set(driver.driver_number, next.length > MAX_TRACE_POINTS ? next.slice(next.length - MAX_TRACE_POINTS) : next)
      changed = true
    }
    if (!changed) return
    // A fresh array each time, so `useSyncExternalStore`'s Object.is check
    // sees the change; the per-driver arrays above are replaced, never mutated.
    this.snapshot = [...this.traces].map(([driver_number, points]) => ({ driver_number, points }))
    for (const listener of this.listeners) listener()
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  getSnapshot = (): Trace[] => this.snapshot
}

export function useTrackTrace(): Trace[] {
  const [accumulator] = useState(() => new TraceAccumulator())

  useEffect(() => {
    const feed = (snapshot: ReturnType<typeof useStore.getState>['snapshot']) =>
      accumulator.observe(snapshot?.drivers ?? [], snapshot?.session?.session_key ?? null)
    feed(useStore.getState().snapshot)
    return useStore.subscribe((state) => feed(state.snapshot))
  }, [accumulator])

  return useSyncExternalStore(accumulator.subscribe, accumulator.getSnapshot)
}
