import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { TrackMap } from './TrackMap'
import { useStore } from '../store'
import type { DriverState, SessionState } from '../types/sessionState'
import sampleSnapshot from '../test/sampleSnapshot.json'

vi.mock('../data/circuits', () => ({
  loadOutline: vi.fn(),
  hasOutline: vi.fn(),
}))

import { loadOutline } from '../data/circuits'

const snapshot = sampleSnapshot as unknown as SessionState
const loadOutlineMock = vi.mocked(loadOutline)

function inject(state: SessionState) {
  useStore.setState({ snapshot: state, status: 'open', lastMessageAt: Date.now() })
}

function withCircuit(circuitKey: number | null, drivers: DriverState[] = snapshot.drivers): SessionState {
  return { ...snapshot, drivers, session: { ...snapshot.session!, circuit_key: circuitKey } }
}

beforeEach(() => {
  useStore.setState({ snapshot: null, status: 'connecting', lastMessageAt: null })
  loadOutlineMock.mockReset()
  loadOutlineMock.mockResolvedValue(null)
})

describe('TrackMap', () => {
  it('draws the bundled outline for a known circuit', async () => {
    loadOutlineMock.mockResolvedValue({
      circuit_key: 39,
      circuit_short_name: 'Monza',
      source_session_key: 1,
      points: [
        [0, 0],
        [100, 0],
        [100, 50],
        [0, 50],
      ],
    })
    inject(withCircuit(39))
    render(<TrackMap />)
    await waitFor(() => expect(screen.getByTestId('outline')).toBeInTheDocument())
    expect(loadOutlineMock).toHaveBeenCalledWith(39)
    expect(screen.getByTestId('outline').getAttribute('d')).toBe('M0 0 L100 0 L100 -50 L0 -50 Z')
    expect(screen.getByText('Monza')).toBeInTheDocument()
  })

  it('draws one dot per driver with coordinates, none for a driver without', () => {
    inject(withCircuit(null))
    render(<TrackMap />)
    const withXY = snapshot.drivers.filter((d) => d.x != null && d.y != null)
    expect(withXY.length).toBe(snapshot.drivers.length - 1)
    expect(screen.getByTestId('track-map').querySelectorAll('g[data-driver]')).toHaveLength(withXY.length)
    const pitted = snapshot.drivers.find((d) => d.in_pit)!
    expect(pitted.x).toBeNull()
    expect(screen.getByTestId('track-map').querySelector(`g[data-driver="${pitted.driver_number}"]`)).toBeNull()
  })

  it('dims a driver in the pits', () => {
    const drivers = snapshot.drivers.map((d, i) => (i === 0 ? { ...d, in_pit: true, x: 1, y: 1 } : d))
    inject(withCircuit(null, drivers))
    render(<TrackMap />)
    const group = screen.getByTestId('track-map').querySelector(`g[data-driver="${drivers[0].driver_number}"]`)!
    expect(group.getAttribute('data-in-pit')).toBe('true')
  })

  it('falls back to tracing car positions when there is no outline', async () => {
    const shift = (by: number, at: string) =>
      snapshot.drivers.map((d) => (d.x == null ? d : { ...d, x: d.x + by, location_at: at }))

    inject(withCircuit(null))
    render(<TrackMap />)
    expect(screen.getByText('tracing')).toBeInTheDocument()
    expect(screen.queryByTestId('outline')).toBeNull()
    // One position per car is a dot, not a line: nothing to draw yet.
    expect(screen.queryAllByTestId('trace')).toHaveLength(0)

    act(() => inject(withCircuit(null, shift(10, '2026-09-04T11:46:00+00:00'))))
    const cars = snapshot.drivers.filter((d) => d.x != null).length
    await waitFor(() => expect(screen.getAllByTestId('trace')).toHaveLength(cars))
    const before = screen.getAllByTestId('trace').map((path) => path.getAttribute('d')!.length)

    act(() => inject(withCircuit(null, shift(20, '2026-09-04T11:47:00+00:00'))))
    await waitFor(() => {
      const after = screen.getAllByTestId('trace').map((path) => path.getAttribute('d')!.length)
      expect(after.every((len, i) => len > before[i])).toBe(true)
    })
  })

  it('traces each car on its own line, never joining one car to another', async () => {
    // The bug this pins: a single shared list of points interleaves the cars,
    // so one polyline zigzags between them instead of drawing the circuit.
    const two: DriverState[] = [
      { ...snapshot.drivers[0], driver_number: 1, x: 0, y: 0, in_pit: false, location_at: 'a' },
      { ...snapshot.drivers[1], driver_number: 44, x: 5000, y: 5000, in_pit: false, location_at: 'a' },
    ]
    inject(withCircuit(null, two))
    render(<TrackMap />)
    const moved: DriverState[] = [
      { ...two[0], x: 10, y: 10, location_at: 'b' },
      { ...two[1], x: 5010, y: 5010, location_at: 'b' },
    ]
    act(() => inject(withCircuit(null, moved)))

    await waitFor(() => expect(screen.getAllByTestId('trace')).toHaveLength(2))
    const paths = screen.getAllByTestId('trace')
    const one = paths.find((p) => p.getAttribute('data-driver') === '1')!
    const other = paths.find((p) => p.getAttribute('data-driver') === '44')!
    expect(one.getAttribute('d')).toBe('M0 0 L10 -10')
    expect(other.getAttribute('d')).toBe('M5000 -5000 L5010 -5010')
    // Neither line may hold a point belonging to the other car.
    expect(one.getAttribute('d')).not.toContain('5000')
    expect(other.getAttribute('d')).not.toContain('M0 0')
  })

  it('draws no car traces once the bundled outline is in', async () => {
    loadOutlineMock.mockResolvedValue({
      circuit_key: 39,
      circuit_short_name: 'Monza',
      source_session_key: 9912,
      points: [
        [0, 0],
        [100, 0],
        [100, 50],
      ],
    })
    inject(withCircuit(39))
    render(<TrackMap />)
    await waitFor(() => expect(screen.getByTestId('outline')).toBeInTheDocument())
    expect(screen.queryAllByTestId('trace')).toHaveLength(0)
  })

  it('shows an empty state before any location arrives', () => {
    const drivers = snapshot.drivers.map((d) => ({ ...d, x: null, y: null, location_at: null }))
    inject(withCircuit(null, drivers))
    render(<TrackMap />)
    expect(screen.getByText('Waiting for location data')).toBeInTheDocument()
    expect(screen.queryByTestId('outline')).toBeNull()
    expect(screen.queryAllByTestId('trace')).toHaveLength(0)
  })
})
