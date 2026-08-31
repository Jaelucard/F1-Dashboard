import { useEffect, useRef } from 'react'
import { useStore } from '../store'
import type { SessionState } from '../types/sessionState'

/**
 * Owns the one WebSocket and feeds the store.
 *
 * Reconnects with capped exponential backoff, because on a race Friday the
 * laptop may sleep, wifi may drop, or the backend may be restarted mid-session
 * and the page has to come back without a manual refresh.
 */

const RECONNECT_BASE_MS = 500
const RECONNECT_MAX_MS = 10_000

function socketUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/ws`
}

export function useSessionSocket(): void {
  const socketRef = useRef<WebSocket | null>(null)
  const timerRef = useRef<number | null>(null)
  const attemptsRef = useRef(0)
  const closedByUsRef = useRef(false)

  useEffect(() => {
    const { applySnapshot, setStatus, setReconnectAttempts } = useStore.getState()
    closedByUsRef.current = false

    const connect = () => {
      if (useStore.getState().status !== 'open') setStatus('connecting')
      const socket = new WebSocket(socketUrl())
      socketRef.current = socket

      socket.onopen = () => {
        attemptsRef.current = 0
        setReconnectAttempts(0)
        setStatus('open')
      }

      socket.onmessage = (event: MessageEvent<string>) => {
        try {
          applySnapshot(JSON.parse(event.data) as SessionState)
        } catch {
          // A malformed frame must not kill the socket; the next may be fine.
          console.warn('discarded unparseable frame')
        }
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
}
