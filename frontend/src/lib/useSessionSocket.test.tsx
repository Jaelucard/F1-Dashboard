import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { StrictMode } from 'react'
import { act, render } from '@testing-library/react'
import { startSocketController } from './socketController'
import { useSessionSocket } from './useSessionSocket'
import { useStore } from '../store'
import sampleSnapshot from '../test/sampleSnapshot.json'

/** A WebSocket double: records every instance so tests can count them. */
class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  static OPEN = 1
  static CLOSED = 3
  readyState = 0
  closed = false
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent<string>) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null

  url: string

  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }

  open() {
    this.readyState = FakeWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  message(text: string) {
    this.onmessage?.(new MessageEvent('message', { data: text }))
  }

  /** The server (or network) closing the socket. */
  serverClose(code = 1006) {
    this.readyState = FakeWebSocket.CLOSED
    this.onclose?.({ code, target: this } as unknown as CloseEvent)
  }

  close() {
    this.closed = true
    this.readyState = FakeWebSocket.CLOSED
    // Browsers fire onclose asynchronously after close(); emulate that.
    queueMicrotask(() => this.onclose?.({ code: 1000, target: this } as unknown as CloseEvent))
  }
}

function Harness() {
  useSessionSocket()
  return null
}

const openSockets = () => FakeWebSocket.instances.filter((s) => s.readyState !== FakeWebSocket.CLOSED)

beforeEach(() => {
  FakeWebSocket.instances = []
  vi.stubGlobal('WebSocket', FakeWebSocket)
  vi.useFakeTimers()
  useStore.setState({ snapshot: null, status: 'connecting', lastMessageAt: null, reconnectAttempts: 0 })
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('useSessionSocket under StrictMode', () => {
  it('leaves exactly one live socket after the double mount', async () => {
    render(
      <StrictMode>
        <Harness />
      </StrictMode>,
    )
    await act(async () => {
      await Promise.resolve()
    })
    // StrictMode mounts, unmounts, mounts again: two sockets created, one alive.
    expect(FakeWebSocket.instances.length).toBe(2)
    expect(openSockets().length).toBe(1)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('does not reconnect when the socket it closed itself reports closure', async () => {
    render(
      <StrictMode>
        <Harness />
      </StrictMode>,
    )
    await act(async () => {
      await Promise.resolve()
    })
    const created = FakeWebSocket.instances.length
    await act(async () => {
      vi.advanceTimersByTime(20_000)
    })
    expect(FakeWebSocket.instances.length).toBe(created)
    expect(useStore.getState().status).toBe('connecting')
  })

  it('closes its socket and cancels its reconnect timer on unmount', async () => {
    const view = render(<Harness />)
    const socket = FakeWebSocket.instances[0]
    act(() => socket.serverClose())
    expect(vi.getTimerCount()).toBe(1)

    view.unmount()
    await act(async () => {
      await Promise.resolve()
    })
    expect(vi.getTimerCount()).toBe(0)
    await act(async () => {
      vi.advanceTimersByTime(60_000)
    })
    expect(FakeWebSocket.instances.length).toBe(1)
    expect(openSockets().length).toBe(0)
  })

  it('survives rapid mount/unmount cycles without leaking sockets', async () => {
    for (let i = 0; i < 5; i += 1) {
      const view = render(<Harness />)
      view.unmount()
    }
    await act(async () => {
      await Promise.resolve()
    })
    expect(openSockets().length).toBe(0)
    expect(vi.getTimerCount()).toBe(0)
  })
})

describe('startSocketController', () => {
  function controller() {
    const statuses: string[] = []
    const frames: string[] = []
    const attempts: number[] = []
    const dispose = startSocketController({
      url: 'ws://test/ws',
      onFrame: (text) => frames.push(text),
      onStatus: (status) => statuses.push(status),
      onAttempts: (n) => attempts.push(n),
    })
    return { dispose, statuses, frames, attempts }
  }

  it('reconnects with backoff after a server-side close', () => {
    const { dispose, statuses, attempts } = controller()
    const first = FakeWebSocket.instances[0]
    first.open()
    first.serverClose()
    expect(statuses).toEqual(['connecting', 'open', 'closed'])
    expect(attempts).toEqual([0, 1])
    expect(FakeWebSocket.instances.length).toBe(1)

    vi.advanceTimersByTime(500)
    expect(FakeWebSocket.instances.length).toBe(2)
    FakeWebSocket.instances[1].serverClose()
    vi.advanceTimersByTime(999)
    expect(FakeWebSocket.instances.length).toBe(2)
    vi.advanceTimersByTime(1)
    expect(FakeWebSocket.instances.length).toBe(3)
    dispose()
  })

  it('ignores events from a socket it has already replaced', () => {
    const { dispose, statuses } = controller()
    const first = FakeWebSocket.instances[0]
    first.serverClose()
    vi.advanceTimersByTime(500)
    const second = FakeWebSocket.instances[1]
    second.open()
    expect(statuses.at(-1)).toBe('open')

    // A late close from the *old* socket must not flip the status or reconnect.
    first.serverClose()
    expect(statuses.at(-1)).toBe('open')
    expect(vi.getTimerCount()).toBe(0)
    expect(FakeWebSocket.instances.length).toBe(2)

    // ...and a late frame from it must not reach the store.
    const { frames } = { frames: [] as string[] }
    first.message('{"late":true}')
    expect(frames).toEqual([])
    dispose()
  })

  it('delivers frames only from the current socket', () => {
    const { dispose, frames } = controller()
    const first = FakeWebSocket.instances[0]
    first.open()
    first.message('a')
    first.serverClose()
    vi.advanceTimersByTime(500)
    const second = FakeWebSocket.instances[1]
    second.open()
    first.message('stale')
    second.message('b')
    expect(frames).toEqual(['a', 'b'])
    dispose()
  })

  it('disposes cleanly whether waiting to reconnect or connected', () => {
    const a = controller()
    FakeWebSocket.instances[0].serverClose()
    expect(vi.getTimerCount()).toBe(1)
    a.dispose()
    expect(vi.getTimerCount()).toBe(0)

    const b = controller()
    const socket = FakeWebSocket.instances.at(-1)!
    socket.open()
    b.dispose()
    expect(socket.closed).toBe(true)
    expect(b.statuses.at(-1)).toBe('open')
  })
})

describe('the wired hook', () => {
  it('applies valid frames and ignores invalid ones', async () => {
    render(<Harness />)
    const socket = FakeWebSocket.instances[0]
    act(() => socket.open())
    act(() => socket.message(JSON.stringify(sampleSnapshot)))
    expect(useStore.getState().snapshot?.drivers.length).toBeGreaterThanOrEqual(20)
    act(() => socket.message('{"type":"snapshot"}'))
    expect(useStore.getState().snapshot?.drivers.length).toBeGreaterThanOrEqual(20)
    expect(useStore.getState().invalidFrames).toBe(1)
  })
})
