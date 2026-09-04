import { beforeEach, describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SectorBests } from './SectorBests'
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

describe('SectorBests', () => {
  it('renders three columns headed S1, S2, S3', () => {
    inject()
    render(<SectorBests />)
    const panel = screen.getByTestId('sector-bests')
    expect(panel.querySelector('[data-col="s1"]')).toBeInTheDocument()
    expect(panel.querySelector('[data-col="s2"]')).toBeInTheDocument()
    expect(panel.querySelector('[data-col="s3"]')).toBeInTheDocument()
  })

  it('lists entries in rank order with rank 1 styled', () => {
    const leaders = [
      [
        { driver_number: 1, name_acronym: 'AAA', team_colour: '111111', time: 26.0 },
        { driver_number: 2, name_acronym: 'BBB', team_colour: '222222', time: 26.5 },
        { driver_number: 3, name_acronym: 'CCC', team_colour: '333333', time: 27.0 },
      ],
      [],
      [],
    ]
    inject({ ...snapshot, sector_leaders: leaders })
    render(<SectorBests />)
    const column = screen.getByTestId('sector-bests').querySelector('[data-col="s1"]')!
    const names = Array.from(column.querySelectorAll('span')).map((el) => el.textContent)
    expect(names.join(' ')).toContain('AAA')
    expect(column.textContent).toContain('26.000')
    expect(column.textContent).toContain('26.500')
    expect(column.textContent).toContain('27.000')
    // Rank 1's entry is the first row rendered.
    const rows = column.querySelectorAll('.flex.items-center.gap-1\\.5')
    expect(rows[0]).toHaveTextContent('AAA')
    const rank1Text = rows[0].querySelector('span')!
    expect(rank1Text.className).toContain('text-timing-best')
  })

  it('shows a placeholder for an empty sector', () => {
    inject({ ...snapshot, sector_leaders: [[], [], []] })
    render(<SectorBests />)
    const s1 = screen.getByTestId('sector-bests').querySelector('[data-col="s1"]')!
    expect(s1).toHaveTextContent('—')
  })
})
