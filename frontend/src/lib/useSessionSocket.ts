import { useEffect } from 'react'
import { useStore } from '../store'
import { startSocketController } from './socketController'
import { accessToken } from './token'

/**
 * Wires the one WebSocket to the store.
 *
 * The lifecycle lives in `socketController.ts`; this hook only starts it on
 * mount and disposes it on unmount. Under React StrictMode the effect runs
 * twice (mount, unmount, mount): the first controller is disposed before the
 * second starts, so exactly one socket survives and no timer leaks.
 */


export function socketUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const token = accessToken()
  const query = token ? `?token=${encodeURIComponent(token)}` : ''
  return `${protocol}//${window.location.host}/ws${query}`
}

export function useSessionSocket(): void {
  useEffect(() => {
    const { applyFrame, setStatus, setReconnectAttempts } = useStore.getState()
    return startSocketController({
      url: socketUrl(),
      onFrame: (text) => {
        applyFrame(text)
      },
      onStatus: setStatus,
      onAttempts: setReconnectAttempts,
    })
  }, [])
}
