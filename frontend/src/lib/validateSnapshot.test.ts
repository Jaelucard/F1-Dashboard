import { describe, expect, it } from 'vitest'
import { parseSnapshotFrame, validateSnapshot } from './validateSnapshot'
import sampleSnapshot from '../test/sampleSnapshot.json'

const valid = JSON.stringify(sampleSnapshot)

describe('parseSnapshotFrame', () => {
  it('accepts the exact bytes the backend sends', () => {
    const result = parseSnapshotFrame(valid)
    expect(result.ok).toBe(true)
    if (result.ok) expect(result.snapshot.drivers.length).toBeGreaterThanOrEqual(20)
  })

  it('rejects unparseable text without throwing', () => {
    const result = parseSnapshotFrame('{not json')
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.reason).toMatch(/json/i)
  })

  it('rejects a frame that is not a snapshot envelope', () => {
    for (const frame of ['null', '42', '"snapshot"', '[]', '{}', '{"type":"ping"}']) {
      const result = parseSnapshotFrame(frame)
      expect(result.ok, frame).toBe(false)
    }
  })
})

describe('validateSnapshot', () => {
  const base = () => JSON.parse(valid) as Record<string, unknown>

  it.each([
    ['phase', 'two'],
    ['mode', 'LIVE'],
    ['mode', 'stream'],
    ['server_time', 123],
    ['credentials_present', 'yes'],
    ['drivers', null],
    ['drivers', {}],
    ['session', 'Monza'],
    ['race_control', null],
    ['track_flag', 7],
    ['partial_aero', 'no'],
    ['session_best_lap', '79.5'],
    ['recorder', 'ok'],
    ['feed', []],
    ['degraded', 'yes'],
  ])('rejects a wrong-typed envelope field: %s = %j', (field, value) => {
    const frame = { ...base(), [field]: value }
    const result = validateSnapshot(frame)
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.reason).toContain(field)
  })

  it('rejects a driver without a numeric driver_number', () => {
    const frame = base()
    const drivers = frame.drivers as Record<string, unknown>[]
    drivers[3] = { ...drivers[3], driver_number: '44' }
    const result = validateSnapshot(frame)
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.reason).toContain('driver_number')
  })

  it('rejects a driver whose timing fields have the wrong type', () => {
    const frame = base()
    const drivers = frame.drivers as Record<string, unknown>[]
    drivers[0] = { ...drivers[0], last_lap_duration: '1:19.681' }
    expect(validateSnapshot(frame).ok).toBe(false)
  })

  it('accepts the awkward real-world values', () => {
    const frame = base()
    const drivers = frame.drivers as Record<string, unknown>[]
    drivers[0] = { ...drivers[0], gap_to_leader: '+1 LAP', interval_ahead: null, team_colour: null, position: null }
    expect(validateSnapshot(frame).ok).toBe(true)
  })

  it('fills in optional blocks that are absent so older backends still render', () => {
    const frame = base()
    delete frame.feed
    delete frame.adapter
    delete frame.degraded
    delete frame.degraded_reason
    const result = validateSnapshot(frame)
    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.snapshot.feed).toBeNull()
      expect(result.snapshot.degraded).toBe(false)
    }
  })

  it('rejects a feed block with an unknown state', () => {
    const frame = { ...base(), feed: { state: 'excellent' } }
    expect(validateSnapshot(frame).ok).toBe(false)
  })

  it('requires the envelope fields the UI depends on', () => {
    for (const field of ['type', 'phase', 'mode', 'server_time', 'drivers']) {
      const frame = base()
      delete frame[field]
      expect(validateSnapshot(frame).ok, field).toBe(false)
    }
  })
})
