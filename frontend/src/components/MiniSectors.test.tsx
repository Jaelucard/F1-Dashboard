import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MiniSectors } from './MiniSectors'
import { SEGMENT_COLOUR, SEGMENT_UNKNOWN_COLOUR, segmentColour } from '../lib/segments'

function bars(): HTMLElement[] {
  return Array.from(screen.getByTestId('mini-sectors').querySelectorAll('span'))
}

describe('MiniSectors', () => {
  it('draws one bar per code, however many there are', () => {
    // Length varies by circuit and by sector: 7 to 9 is typical, and nothing
    // may assume a fixed count.
    for (const length of [7, 8, 9, 3, 20]) {
      const { unmount } = render(<MiniSectors segments={Array(length).fill(2049)} />)
      expect(bars()).toHaveLength(length)
      unmount()
    }
  })

  it('colours each bar by its code', () => {
    render(<MiniSectors segments={[2048, 2049, 2051, 2064]} />)
    expect(bars().map((b) => b.style.backgroundColor)).toEqual([
      SEGMENT_COLOUR[2048],
      SEGMENT_COLOUR[2049],
      SEGMENT_COLOUR[2051],
      SEGMENT_COLOUR[2064],
    ])
  })

  it('renders an undocumented code neutrally rather than guessing at it', () => {
    // 2050, 2052 and 2068 are undocumented, and OpenF1 may add more. Showing
    // one as green or purple would be inventing timing information.
    render(<MiniSectors segments={[2050, 2052, 2068, 0, 9999]} />)
    for (const bar of bars()) {
      expect(bar.style.backgroundColor).toBe(SEGMENT_UNKNOWN_COLOUR)
    }
    expect(segmentColour(2050)).toBe(SEGMENT_UNKNOWN_COLOUR)
    expect(segmentColour(2049)).toBe(SEGMENT_COLOUR[2049])
  })

  it('keeps the strip container with no bars for an empty array', () => {
    // The height has to survive a lap reset, or every row jumps.
    render(<MiniSectors segments={[]} />)
    const strip = screen.getByTestId('mini-sectors')
    expect(strip).toBeInTheDocument()
    expect(strip.querySelectorAll('span')).toHaveLength(0)
    expect(strip.className).toContain('h-[3px]')
  })

  it('is labelled, and lists its codes for debugging', () => {
    render(<MiniSectors segments={[2049, 2051]} />)
    const strip = screen.getByLabelText('mini-sectors')
    expect(strip.getAttribute('title')).toBe('2049 2051')
    expect(bars().map((b) => b.getAttribute('data-segment'))).toEqual(['2049', '2051'])
  })
})
