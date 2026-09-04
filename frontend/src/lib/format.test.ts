import { describe, expect, it } from 'vitest'
import {
  formatDataAge,
  formatGap,
  formatInterval,
  formatLapTime,
  formatSessionClock,
  formatTyreAge,
  lapTimeColour,
  NO_DATA,
  sectorColour,
} from './format'

describe('formatLapTime', () => {
  it('renders a lap as M:SS.mmm', () => {
    expect(formatLapTime(79.681)).toBe('1:19.681')
    expect(formatLapTime(88.323)).toBe('1:28.323')
    expect(formatLapTime(125.5)).toBe('2:05.500')
  })

  it('renders a sector time without a leading minute', () => {
    expect(formatLapTime(30.184)).toBe('30.184')
    expect(formatLapTime(9.2)).toBe('9.200')
  })

  it('pads seconds so columns line up', () => {
    // 61.5s must be 1:01.500, not 1:1.500 - a ragged column is unreadable.
    expect(formatLapTime(61.5)).toBe('1:01.500')
  })

  it('always shows three decimals, because thousandths decide sessions', () => {
    expect(formatLapTime(80)).toBe('1:20.000')
  })

  it('shows no data rather than a wrong number', () => {
    expect(formatLapTime(null)).toBe(NO_DATA)
    expect(formatLapTime(undefined)).toBe(NO_DATA)
    expect(formatLapTime(NaN)).toBe(NO_DATA)
    expect(formatLapTime(-1)).toBe(NO_DATA)
  })
})

describe('formatGap', () => {
  it('shows LEADER for the car in front', () => {
    expect(formatGap(0, true)).toBe('LEADER')
    // A leader is a leader even if a gap value happens to be present.
    expect(formatGap(1.234, true)).toBe('LEADER')
  })

  it('formats a numeric gap as +1.234', () => {
    expect(formatGap(1.234)).toBe('+1.234')
    expect(formatGap(0.423)).toBe('+0.423')
    expect(formatGap(0)).toBe('+0.000')
    expect(formatGap(12.5)).toBe('+12.500')
  })

  it('passes through the lapped-car string OpenF1 sends', () => {
    // Confirmed against real 2025 race data: this really arrives as a string.
    expect(formatGap('+1 LAP')).toBe('+1 LAP')
    expect(formatGap('+2 LAPS')).toBe('+2 LAPS')
  })

  it('adds a plus to a bare string form', () => {
    expect(formatGap('1 LAP')).toBe('+1 LAP')
  })

  it('shows no data for null, which happens early in a session', () => {
    expect(formatGap(null)).toBe(NO_DATA)
    expect(formatGap('   ')).toBe(NO_DATA)
    expect(formatGap(NaN)).toBe(NO_DATA)
  })
})

describe('formatInterval', () => {
  it('formats the interval to the car ahead', () => {
    expect(formatInterval(0.263)).toBe('+0.263')
    expect(formatInterval('+1 LAP')).toBe('+1 LAP')
    expect(formatInterval(null)).toBe(NO_DATA)
  })

  it('never says LEADER: every car has someone ahead except P1', () => {
    expect(formatInterval(0)).toBe('+0.000')
  })
})

describe('formatTyreAge', () => {
  it('renders laps on the set', () => {
    expect(formatTyreAge(12)).toBe('12L')
  })

  it('renders a brand new set as 0L, not as missing data', () => {
    expect(formatTyreAge(0)).toBe('0L')
  })

  it('shows no data when the stint is unknown', () => {
    expect(formatTyreAge(null)).toBe(NO_DATA)
  })
})

describe('lapTimeColour', () => {
  it('is purple for the session best', () => {
    expect(lapTimeColour(79.5, 79.5, 79.5)).toBe('best')
  })

  it('is green for a personal best that is not the session best', () => {
    expect(lapTimeColour(80.1, 80.1, 79.5)).toBe('personal')
  })

  it('is yellow for a lap slower than the driver own best', () => {
    expect(lapTimeColour(81.0, 80.1, 79.5)).toBe('slower')
  })

  it('is white when there is no lap', () => {
    expect(lapTimeColour(null, 80.1, 79.5)).toBe('none')
  })
})

describe('sectorColour', () => {
  it('is purple for the session best', () => {
    expect(sectorColour(26.501, 26.501, 26.501)).toBe('best')
  })

  it('is green for a personal best that is not the session best', () => {
    expect(sectorColour(27.1, 27.1, 26.5)).toBe('personal')
  })

  it('is yellow for a sector slower than the driver own best', () => {
    expect(sectorColour(28.0, 27.1, 26.5)).toBe('slower')
  })

  it('is white when the sector has no time yet', () => {
    expect(sectorColour(null, 27.1, 26.5)).toBe('none')
    expect(sectorColour(undefined, 27.1, 26.5)).toBe('none')
  })

  it('matches within a 0.0005s tolerance, not exact equality', () => {
    // Backend-computed running minimums can drift from the value they should
    // match by float noise; exact equality would wrongly show yellow.
    expect(sectorColour(26.5004, 27.1, 26.5)).toBe('best')
    expect(sectorColour(26.5005, 27.1, 26.5)).toBe('best')
    // just outside the tolerance
    expect(sectorColour(26.5006, 27.1, 26.5)).toBe('slower')
  })

  it('checks the session best before the personal best', () => {
    // A driver's own best that happens to equal the session best must read
    // purple, not green - session best takes priority.
    expect(sectorColour(26.5, 26.5, 26.5)).toBe('best')
  })

  it('has no session best yet: falls through to personal, then slower', () => {
    expect(sectorColour(27.1, 27.1, null)).toBe('personal')
    expect(sectorColour(27.1, null, null)).toBe('slower')
  })
})

describe('formatDataAge', () => {
  it('counts seconds while the feed is healthy', () => {
    expect(formatDataAge(0)).toBe('0s')
    expect(formatDataAge(4)).toBe('4s')
  })

  it('switches to minutes once something is clearly wrong', () => {
    expect(formatDataAge(65)).toBe('1m05s')
    expect(formatDataAge(600)).toBe('10m00s')
  })

  it('shows no data before the first frame', () => {
    expect(formatDataAge(null)).toBe(NO_DATA)
  })
})

describe('formatSessionClock', () => {
  const start = '2026-09-04T11:30:00+00:00'

  it('counts up from the session start', () => {
    expect(formatSessionClock(start, new Date('2026-09-04T11:45:30Z'))).toBe('00:15:30')
    expect(formatSessionClock(start, new Date('2026-09-04T13:00:00Z'))).toBe('01:30:00')
  })

  it('counts down before the session starts', () => {
    expect(formatSessionClock(start, new Date('2026-09-04T11:00:00Z'))).toBe('-00:30:00')
  })

  it('shows no data without a start time', () => {
    expect(formatSessionClock(null)).toBe(NO_DATA)
    expect(formatSessionClock('not a date')).toBe(NO_DATA)
  })
})

describe('formatLapTime rounding at minute boundaries', () => {
  it('rolls 59.9996 over to 1:00.000 rather than printing 60.000', () => {
    expect(formatLapTime(59.9996)).toBe('1:00.000')
    expect(formatLapTime(59.9995)).toBe('1:00.000')
  })

  it('rolls 119.9996 over to 2:00.000 rather than 1:60.000', () => {
    expect(formatLapTime(119.9996)).toBe('2:00.000')
    expect(formatLapTime(179.9997)).toBe('3:00.000')
  })

  it('does not roll over when the thousandths do not round up', () => {
    expect(formatLapTime(59.9994)).toBe('59.999')
    expect(formatLapTime(119.9994)).toBe('1:59.999')
  })

  it('handles exact minutes and zero', () => {
    expect(formatLapTime(60)).toBe('1:00.000')
    expect(formatLapTime(120)).toBe('2:00.000')
    expect(formatLapTime(0)).toBe('0.000')
  })

  it('shows no data for every non-finite or negative input', () => {
    expect(formatLapTime(Infinity)).toBe(NO_DATA)
    expect(formatLapTime(-Infinity)).toBe(NO_DATA)
    expect(formatLapTime(-0.001)).toBe(NO_DATA)
    expect(formatLapTime('79.5' as unknown as number)).toBe(NO_DATA)
  })
})
