import { useEffect, useRef, useState } from 'react'
import type { ConnectionStatus, Snapshot } from './types'

/**
 * Owns the one WebSocket to the backend.
 *
 * Two things here matter beyond Phase 0 and are why this is a hook rather than
 * a few lines in App.tsx:
 *
 * 1. Reconnect with capped exponential backoff. On Friday the laptop may sleep,
 *    wifi may drop, or the backend may be restarted mid-session. The page must
 *    come back on its own without a manual refresh.
 * 2. `lastMessageAt` is recorded on every inbound frame. The StatusStrip's
 *    data-age counter is derived from it, and that counter is the single
 *    clearest signal that the feed is alive.
 */

const RECONNECT_BASE_MS = 500
const RECONNECT_MAX_MS = 10_000

function socketUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/ws`
}

export interface SessionSocket {
  status: ConnectionStatus
  snapshot: Snapshot | null
  /** epoch ms of the last frame received, or null if none yet */
  lastMessageAt: number | null
  reconnectAttempts: number
}

export function useSessionSocket(): SessionSocket {
  const [status, setStatus] = useState<ConnectionStatus>('connecting')
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [lastMessageAt, setLastMessageAt] = useState<number | null>(null)
  const [reconnectAttempts, setReconnectAttempts] = useState(0)

  const socketRef = useRef<WebSocket | null>(null)
  const timerRef = useRef<number | null>(null)
  const attemptsRef = useRef(0)
  const closedByUsRef = useRef(false)

  useEffect(() => {
    closedByUsRef.current = false

    const connect = () => {
      setStatus((current) => (current === 'open' ? current : 'connecting'))
      const socket = new WebSocket(socketUrl())
      socketRef.current = socket

      socket.onopen = () => {
        attemptsRef.current = 0
        setReconnectAttempts(0)
        setStatus('open')
      }

      socket.onmessage = (event: MessageEvent<string>) => {
        setLastMessageAt(Date.now())
        try {
          setSnapshot(JSON.parse(event.data) as Snapshot)
        } catch {
          // A malformed frame must not kill the socket; the next one may be fine.
          console.warn('discarded unparseable frame')
        }
      }

      socket.onerror = () => {
        // onclose always follows, so reconnect is handled there only.
      }

      socket.onclose = () => {
        socketRef.current = null
        if (closedByUsRef.current) return

        setStatus('closed')
        const attempt = attemptsRef.current + 1
        attemptsRef.current = attempt
        setReconnectAttempts(attempt)

        const delay = Math.min(RECONNECT_BASE_MS * 2 ** (attempt - 1), RECONNECT_MAX_MS)
        timerRef.current = window.setTimeout(connect, delay)
      }
    }

    connect()

    return () => {
      closedByUsRef.current = true
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
      socketRef.current?.close()
    }
  }, [])

  return { status, snapshot, lastMessageAt, reconnectAttempts }
}
