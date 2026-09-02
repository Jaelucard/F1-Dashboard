import { beforeEach, describe, expect, it } from 'vitest'
import { useStore } from './store'
import sampleSnapshot from './test/sampleSnapshot.json'

const valid = JSON.stringify(sampleSnapshot)

beforeEach(() => {
  useStore.setState({ snapshot: null, status: 'connecting', lastMessageAt: null, invalidFrames: 0, lastInvalidReason: null })
})

describe('store.applyFrame', () => {
  it('applies a valid frame and stamps the arrival time', () => {
    expect(useStore.getState().applyFrame(valid)).toBe(true)
    const state = useStore.getState()
    expect(state.snapshot?.drivers.length).toBeGreaterThanOrEqual(20)
    expect(state.lastMessageAt).not.toBeNull()
  })

  it('does not let an invalid frame overwrite valid state', () => {
    useStore.getState().applyFrame(valid)
    const before = useStore.getState()
    expect(
      useStore.getState().applyFrame('{"type":"snapshot","phase":2,"mode":"live","server_time":"t","drivers":"nope"}'),
    ).toBe(false)
    const after = useStore.getState()
    expect(after.snapshot).toBe(before.snapshot)
    expect(after.lastMessageAt).toBe(before.lastMessageAt)
    expect(after.invalidFrames).toBe(1)
    expect(after.lastInvalidReason).toContain('drivers')
  })

  it('does not throw on garbage', () => {
    expect(() => useStore.getState().applyFrame('<html>')).not.toThrow()
    expect(useStore.getState().snapshot).toBeNull()
    expect(useStore.getState().invalidFrames).toBe(1)
  })
})
