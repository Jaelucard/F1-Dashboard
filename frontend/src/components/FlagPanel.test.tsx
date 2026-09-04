import { beforeEach, describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { FlagPanel } from './FlagPanel'
import { useStore } from '../store'
import type { SessionState } from '../types/sessionState'
import sampleSnapshot from '../test/sampleSnapshot.json'

const snapshot = sampleSnapshot as unknown as SessionState

function inject(state: SessionState = snapshot) {
  useStore.setState({ snapshot: state, status: 'open', lastMessageAt: Date.now() })
}

beforeEach(() => {
  useStore.setState({ snapshot: null, status: 'connecting', lastMessageAt: null })
})

describe('FlagPanel', () => {
  it('renders the current track flag', () => {
    inject()
    render(<FlagPanel />)
    expect(screen.getByTestId('track-flag')).toHaveTextContent('YELLOW')
  })

  it('shows NO FLAG when the track is green/clear', () => {
    inject({ ...snapshot, track_flag: null })
    render(<FlagPanel />)
    expect(screen.getByTestId('track-flag')).toHaveTextContent('NO FLAG')
  })

  it('renders one row per active sector flag, ordered by sector number', () => {
    inject()
    render(<FlagPanel />)
    const rows = screen.getByTestId('sector-flags').querySelectorAll('li')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveTextContent('S3 DOUBLE YELLOW')
    expect(rows[1]).toHaveTextContent('S7 YELLOW')
  })

  it('renders no sector-flags list when nothing is active', () => {
    inject({ ...snapshot, sector_flags: [] })
    render(<FlagPanel />)
    expect(screen.queryByTestId('sector-flags')).not.toBeInTheDocument()
  })

  it('shows the safety car banner when set', () => {
    inject()
    render(<FlagPanel />)
    expect(screen.getByTestId('safety-car-banner')).toHaveTextContent('VSC DEPLOYED')
  })

  it('shows no safety car banner when clear', () => {
    inject({ ...snapshot, safety_car: null })
    render(<FlagPanel />)
    expect(screen.queryByTestId('safety-car-banner')).not.toBeInTheDocument()
  })

  it('shows the last three driver flags as chips with the driver acronym', () => {
    inject()
    render(<FlagPanel />)
    const chips = screen.getByTestId('driver-flags').children
    expect(chips.length).toBeGreaterThan(0)
    expect(screen.getByTestId('driver-flags')).toHaveTextContent('BLUE')
    expect(screen.getByTestId('driver-flags')).toHaveTextContent('BLACK AND WHITE')
    // LAW is driver 101, PIA is driver 105 in the fixture grid.
    const driverFor = (n: number) => snapshot.drivers.find((d) => d.driver_number === n)!.name_acronym
    expect(screen.getByTestId('driver-flags')).toHaveTextContent(driverFor(101)!)
    expect(screen.getByTestId('driver-flags')).toHaveTextContent(driverFor(105)!)
  })

  it('caps the message list at five, newest first', () => {
    inject()
    render(<FlagPanel />)
    const items = screen.getByTestId('race-control-messages').querySelectorAll('li')
    expect(items.length).toBeLessThanOrEqual(5)
    // The fixture's newest message is the BLACK AND WHITE driver flag.
    expect(items[0]).toHaveTextContent('BLACK AND WHITE FLAG')
  })

  it('shows a placeholder when there are no messages', () => {
    inject({ ...snapshot, race_control: [] })
    render(<FlagPanel />)
    expect(screen.getByTestId('race-control-messages')).toHaveTextContent('No messages yet')
  })

  it('never shows DRS anywhere', () => {
    inject()
    render(<FlagPanel />)
    expect(screen.queryByText(/drs/i)).not.toBeInTheDocument()
  })
})
