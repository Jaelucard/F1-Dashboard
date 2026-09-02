import type { ConnectionStatus } from './types'

/**
 * Owns one WebSocket and its reconnect timer, independent of React.
 *
 * Written as a plain function returning a dispose callback so that the hook
 * is a one-line `useEffect`, and so that the lifecycle can be tested without
 * rendering. The rules it enforces:
 *
 *   - Every callback checks that the socket it fired on is still *the*
 *     socket. A late `close` or `message` from a socket that has already been
 *     replaced (or disposed) is ignored, so it cannot flip the status, deliver
 *     a stale frame, or schedule a second reconnect.
 *   - Dispose closes exactly the current socket and clears exactly the
 *     current timer, then marks the controller dead so nothing runs after it.
 *   - Reconnects use capped exponential backoff, because on a race Friday the
 *     laptop may sleep, wifi may drop, or the backend may restart mid-session.
 */

export const RECONNECT_BASE_MS = 500
export const RECONNECT_MAX_MS = 10_000

/** The subset of WebSocket the controller uses, so tests can substitute one. */
export interface WebSocketLike {
  onopen: ((ev: Event) => void) | null
  onmessage: ((ev: MessageEvent<string>) => void) | null
  onclose: ((ev: CloseEvent) => void) | null
  onerror: ((ev: Event) => void) | null
  close(): void
}

export interface SocketControllerOptions {
  url: string
  onFrame: (text: string) => void
  onStatus: (status: ConnectionStatus) => void
  onAttempts: (attempts: number) => void
  createSocket?: (url: string) => WebSocketLike
}

export function reconnectDelay(attempt: number): number {
  return Math.min(RECONNECT_BASE_MS * 2 ** (attempt - 1), RECONNECT_MAX_MS)
}

export function startSocketController(options: SocketControllerOptions): () => void {
  const createSocket = options.createSocket ?? ((url: string) => new WebSocket(url))

  const state: {
    socket: WebSocketLike | null
    timer: number | null
    attempts: number
    disposed: boolean
  } = { socket: null, timer: null, attempts: 0, disposed: false }

  const connect = () => {
    if (state.disposed) return
    state.timer = null
    options.onStatus('connecting')

    const socket = createSocket(options.url)
    state.socket = socket
    const isCurrent = () => !state.disposed && state.socket === socket

    socket.onopen = () => {
      if (!isCurrent()) return
      state.attempts = 0
      options.onAttempts(0)
      options.onStatus('open')
    }

    socket.onmessage = (event: MessageEvent<string>) => {
      if (!isCurrent()) return
      options.onFrame(event.data)
    }

    socket.onerror = () => {
      // The close event that follows carries the decision; nothing to do here.
    }

    socket.onclose = () => {
      if (!isCurrent()) return
      state.socket = null
      options.onStatus('closed')
      state.attempts += 1
      options.onAttempts(state.attempts)
      state.timer = window.setTimeout(connect, reconnectDelay(state.attempts))
    }
  }

  connect()

  return () => {
    if (state.disposed) return
    state.disposed = true
    if (state.timer !== null) {
      window.clearTimeout(state.timer)
      state.timer = null
    }
    const socket = state.socket
    state.socket = null
    if (socket !== null) {
      // Detach first so the browser's asynchronous close event is a no-op.
      socket.onopen = null
      socket.onmessage = null
      socket.onclose = null
      socket.onerror = null
      try {
        socket.close()
      } catch {
        // Already closed; nothing to release.
      }
    }
  }
}
