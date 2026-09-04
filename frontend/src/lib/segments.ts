/**
 * OpenF1 mini-sector status codes and their colours.
 *
 * Lives apart from `MiniSectors.tsx` so that component exports only a
 * component (oxlint's `react(only-export-components)`), and so the map can be
 * asserted on directly.
 *
 * Confirmed against Monza 2026 FP1: every one of 545 laps carried segments,
 * and the codes seen were 2048, 2049, 2051, 2064 and 0. An unknown code renders
 * neutral rather than being guessed at - 2050, 2052 and 2068 are undocumented
 * and OpenF1 may add more, so showing one as green or purple would be
 * inventing timing information.
 */

export const SEGMENT_COLOUR: Record<number, string> = {
  2048: 'var(--color-timing-slower)', // yellow
  2049: 'var(--color-timing-personal)', // green
  2051: 'var(--color-timing-best)', // purple
  2064: 'var(--color-f1-muted)', // pit lane
}

/** 0 is OpenF1's own "not available", and shares this with any unknown code. */
export const SEGMENT_UNKNOWN_COLOUR = 'var(--color-f1-line)'

export function segmentColour(code: number): string {
  return SEGMENT_COLOUR[code] ?? SEGMENT_UNKNOWN_COLOUR
}
