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
    inject(withCircuit(null))
    render(<TrackMap />)
    await waitFor(() => expect(screen.getByTestId('outline')).toBeInTheDocument())
    expect(screen.getByText('tracing')).toBeInTheDocument()
    const before = screen.getByTestId('outline').getAttribute('d')!
    const moved = snapshot.drivers.map((d) =>
      d.x == null ? d : { ...d, x: d.x + 10, location_at: '2026-09-04T11:46:00+00:00' },
    )
    act(() => inject(withCircuit(null, moved)))
    await waitFor(() => expect(screen.getByTestId('outline').getAttribute('d')!.length).toBeGreaterThan(before.length))
  })

  it('shows an empty state before any location arrives', () => {
    const drivers = snapshot.drivers.map((d) => ({ ...d, x: null, y: null, location_at: null }))
    inject(withCircuit(null, drivers))
    render(<TrackMap />)
    expect(screen.getByText('Waiting for location data')).toBeInTheDocument()
    expect(screen.queryByTestId('outline')).toBeNull()
  })
})
